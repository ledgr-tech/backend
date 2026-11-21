"""A empresa removida some de tudo, como se não existisse (issue #215, ADR-019).

A regra mora num ponto só, o filtro de sessão de app/models/empresa.py. Estes
testes provam que ela chega a cada porta de entrada:

- as rotas por empresa (`X-Ledgr-Empresa`), descobertas por `app.routes` pelas
  dependencies `obter_empresa_ativa` e `obter_empresa_id_ativa`, para nenhuma
  rota nova escapar: com a empresa removida, todas respondem 404;
- o assistente (que só usa o cabeçalho com dados ligados), a `/v1`, o MCP, o
  link público do relatório, as chaves de API, a explicação automática e
  qualquer consulta ORM de `Empresa` (um select, um `db.get`, uma contagem).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import cabecalhos
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_assistente_dados import assistente  # noqa: F401 - fixture
from test_links_relatorio import INVALIDO, _consultar, _criar, conta  # noqa: F401 - fixture
from test_links_relatorio import _flag_ligada as _link_publico_ligado  # noqa: F401 - autouse
from test_mcp_oauth import RECURSO, mcp  # noqa: F401 - fixture

from app.api import admin
from app.core import conta_inativa
from app.core.auth import (
    CABECALHO_EMPRESA,
    PREFIXO_TOKEN_MCP,
    autenticar_token_mcp,
    obter_empresa_ativa,
    obter_empresa_id_ativa,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    ChaveApi,
    ClienteOAuth,
    ConexaoMcp,
    Conta,
    Empresa,
    EmpresaFavorita,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    TokenOAuth,
)
from app.services import plano
from app.services.ajuda import consultas
from app.services.ia import geracao
from main import app

client = TestClient(app)

NAO_ENCONTRADA = {"detail": "Empresa não encontrada."}
ROTAS_DA_EMPRESA_ATIVA = {obter_empresa_ativa, obter_empresa_id_ativa}


@pytest.fixture
def equipe(db_session, criar_usuario, criar_empresa, gerar_token):
    """Admin da conta, a empresa principal e uma cliente removida pela API."""
    admin = criar_usuario(nome="Ana Admin")
    principal = db_session.scalars(select(Empresa).where(Empresa.conta_id == admin.conta_id)).one()
    cliente = criar_empresa("Cliente Removida Ltda", conta_id=admin.conta_id)
    headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id))
    return SimpleNamespace(
        admin=admin,
        conta_id=admin.conta_id,
        headers=headers,
        principal=principal,
        cliente=cliente,
        com_empresa=lambda empresa: {**headers, CABECALHO_EMPRESA: str(empresa.id)},
        remover=lambda: client.delete(f"/empresas/{cliente.id}", headers=headers),
        restaurar=lambda: client.post(f"/empresas/{cliente.id}/restaurar", headers=headers),
    )


# --- todas as rotas por empresa --------------------------------------------------------


def _depende_de(dependant, alvos) -> bool:
    return any(d.call in alvos or _depende_de(d, alvos) for d in dependant.dependencies)


def _caminho(rota) -> str:
    montada = getattr(rota, "starlette_route", None)
    return getattr(montada, "path", None) or rota.path or ""


def rotas_por_empresa() -> list[tuple[str, str]]:
    achadas = set()
    for rota in iter_route_contexts(app.routes):
        if not isinstance(rota.original_route, APIRoute):
            continue
        if _depende_de(rota.dependant, ROTAS_DA_EMPRESA_ATIVA):
            for metodo in rota.methods - {"HEAD", "OPTIONS"}:
                achadas.add((metodo, _caminho(rota)))
    return sorted(achadas)


def _preencher(caminho: str) -> str:
    """Um valor qualquer para cada parâmetro do caminho: a dependency da empresa
    roda antes de qualquer validação do endpoint."""
    for parte in {p for p in caminho.split("/") if p.startswith("{")}:
        nome = parte.strip("{}")
        caminho = caminho.replace(parte, "2026-09" if nome == "competencia" else str(uuid.uuid4()))
    return caminho


def test_a_descoberta_acha_as_rotas_por_empresa_conhecidas():
    # Sem isto, o teste parametrizado passaria vazio se a busca deixasse de
    # achar as rotas (mudança no FastAPI ou na dependency).
    achadas = set(rotas_por_empresa())

    assert len(achadas) >= 25
    for conhecida in (
        ("GET", "/extratos"),
        ("POST", "/extratos/upload"),
        ("POST", "/conciliacoes"),
        ("POST", "/fechamentos"),
        ("GET", "/execucoes"),
        ("GET", "/busca"),
        ("PUT", "/empresa/configuracoes"),
        ("POST", "/importacoes-ia"),
        ("POST", "/explicacoes"),
    ):
        assert conhecida in achadas, conhecida


@pytest.mark.parametrize(
    ("metodo", "caminho"), rotas_por_empresa(), ids=lambda v: v if isinstance(v, str) else None
)
def test_com_a_empresa_removida_toda_rota_por_empresa_responde_404(equipe, metodo, caminho):
    url = _preencher(caminho)
    assert equipe.remover().status_code == 200

    resposta = client.request(metodo, url, headers=equipe.com_empresa(equipe.cliente), json={})

    assert resposta.status_code == 404, f"{metodo} {caminho}: {resposta.text}"
    assert resposta.json() == NAO_ENCONTRADA
    # A mesma rota, com uma empresa ativa, não dá esse 404: o teste não passa por
    # um caminho que nunca chegaria à empresa.
    ativa = client.request(metodo, url, headers=equipe.com_empresa(equipe.principal), json={})
    assert ativa.json().get("detail") != NAO_ENCONTRADA["detail"], f"{metodo} {caminho}"


# --- assistente ------------------------------------------------------------------------


def test_assistente_com_a_empresa_removida_da_404_e_nada_vai_ao_provedor(
    equipe,
    assistente,  # noqa: F811
):
    assert equipe.remover().status_code == 200

    resposta = client.post(
        "/assistente/perguntas",
        headers=equipe.com_empresa(equipe.cliente),
        json={"pergunta": "como ficou setembro?"},
    )

    assert resposta.status_code == 404
    assert resposta.json() == NAO_ENCONTRADA


def test_o_nome_da_empresa_removida_continua_sendo_mascarado_para_o_provedor(db_session, equipe):
    """Ao contrário das listas, aqui enxergar a removida é o comportamento
    seguro: o histórico da conversa ainda pode citar o nome dela."""
    equipe.remover()

    nomes = consultas.nomes_das_empresas_da_conta(db_session, equipe.conta_id)

    assert "Cliente Removida Ltda" in nomes
    assert len(nomes) == 2


def test_conta_inativa_vale_tambem_para_a_empresa_removida(db_session, equipe):
    cliente_id = equipe.cliente.id
    equipe.remover()
    db_session.execute(
        update(Conta)
        .where(Conta.id == equipe.conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()

    assert conta_inativa.conta_da_empresa_esta_inativa(db_session, cliente_id) is True


def test_chave_de_empresa_removida_e_revogada_e_sai_do_teto_de_chaves_ativas(
    db_session, equipe, monkeypatch
):
    from app.api import chaves_api

    monkeypatch.setattr(chaves_api, "MAXIMO_CHAVES_ATIVAS", 2)
    client.post(
        "/chaves-api",
        headers=equipe.headers,
        json={"nome": "Da cliente", "empresa_id": str(equipe.cliente.id)},
    )
    client.post("/chaves-api", headers=equipe.headers, json={"nome": "Da conta"})
    assert (
        client.post("/chaves-api", headers=equipe.headers, json={"nome": "Mais"}).status_code == 409
    )

    equipe.remover()

    assert (
        client.post("/chaves-api", headers=equipe.headers, json={"nome": "Mais"}).status_code == 201
    )


# --- API pública (/v1) -----------------------------------------------------------------


def _v1(token, caminho, **params):
    return client.get(f"/v1{caminho}", headers={"Authorization": f"Bearer {token}"}, params=params)


def test_v1_da_404_na_empresa_removida_e_nao_a_lista(equipe):
    chave = client.post("/chaves-api", headers=equipe.headers, json={"nome": "Integração"}).json()
    token = chave["chave"]
    assert {e["id"] for e in _v1(token, "/empresas").json()["itens"]} == {
        str(equipe.principal.id),
        str(equipe.cliente.id),
    }
    assert _v1(token, f"/empresas/{equipe.cliente.id}/fechamentos").status_code == 200

    equipe.remover()

    assert {e["id"] for e in _v1(token, "/empresas").json()["itens"]} == {str(equipe.principal.id)}
    for caminho in ("resumo", "conciliacoes", "divergencias", "fechamentos"):
        resposta = _v1(token, f"/empresas/{equipe.cliente.id}/{caminho}", competencia="2026-09")
        assert resposta.status_code == 404, caminho
        assert resposta.json() == NAO_ENCONTRADA
    assert _v1(token, f"/empresas/{equipe.principal.id}/fechamentos").status_code == 200

    equipe.restaurar()
    assert _v1(token, f"/empresas/{equipe.cliente.id}/fechamentos").status_code == 200


def test_chave_de_api_da_empresa_removida_e_revogada_e_nao_lista(equipe):
    criada = client.post(
        "/chaves-api",
        headers=equipe.headers,
        json={"nome": "Só da cliente", "empresa_id": str(equipe.cliente.id)},
    ).json()
    da_conta = client.post("/chaves-api", headers=equipe.headers, json={"nome": "Da conta"}).json()
    assert len(client.get("/chaves-api", headers=equipe.headers).json()["itens"]) == 2

    equipe.remover()

    itens = client.get("/chaves-api", headers=equipe.headers).json()["itens"]
    assert [i["id"] for i in itens] == [da_conta["id"]]
    assert _v1(criada["chave"], "/empresas").status_code == 401
    assert _v1(da_conta["chave"], "/empresas").status_code == 200
    nova = client.post(
        "/chaves-api",
        headers=equipe.headers,
        json={"nome": "Nova", "empresa_id": str(equipe.cliente.id)},
    )
    assert nova.status_code == 404

    # Volta para a lista revogada: quem usava cria outra.
    equipe.restaurar()
    itens = {i["id"]: i for i in client.get("/chaves-api", headers=equipe.headers).json()["itens"]}
    assert set(itens) == {criada["id"], da_conta["id"]}
    assert itens[criada["id"]]["revogada_em"] is not None
    assert itens[da_conta["id"]]["revogada_em"] is None
    assert _v1(criada["chave"], "/empresas").status_code == 401
    assert db_chave(criada["id"]) is not None


def db_chave(chave_id):
    with SessionLocal() as db:
        return db.get(ChaveApi, uuid.UUID(chave_id))


# --- MCP -------------------------------------------------------------------------------


def _token_mcp(db_session, equipe, empresa) -> str:
    cliente_id = f"cliente-{uuid.uuid4().hex[:8]}"
    db_session.add(
        ClienteOAuth(
            client_id=cliente_id, client_name="Claude", redirect_uris=["https://x.test/cb"]
        )
    )
    db_session.flush()
    conexao = ConexaoMcp(
        conta_id=equipe.conta_id,
        empresa_id=empresa.id,
        client_id=cliente_id,
        nome_cliente="Claude",
        escopo="ledgr.leitura",
        criada_por_id=equipe.admin.id,
    )
    db_session.add(conexao)
    db_session.flush()
    token = f"{PREFIXO_TOKEN_MCP}{uuid.uuid4().hex}"
    db_session.add(
        TokenOAuth(
            conexao_id=conexao.id,
            client_id=cliente_id,
            hash_acesso=hashlib.sha256(token.encode()).hexdigest(),
            scope="ledgr.leitura",
            resource=RECURSO,
            acesso_expira_em=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db_session.commit()
    return token


def _autenticar(token):
    with SessionLocal() as db:
        return autenticar_token_mcp(db, token)


@pytest.mark.usefixtures("mcp")
def test_token_mcp_da_empresa_removida_para_de_valer_e_nao_volta_na_restauracao(db_session, equipe):
    token = _token_mcp(db_session, equipe, equipe.cliente)
    outro = _token_mcp(db_session, equipe, equipe.principal)
    assert _autenticar(token)[0].empresa_id == equipe.cliente.id

    equipe.remover()

    for invalido in (token,):
        with pytest.raises(HTTPException) as erro:
            _autenticar(invalido)
        assert erro.value.status_code == 401
    assert _autenticar(outro)[0].empresa_id == equipe.principal.id
    equipe.restaurar()
    with pytest.raises(HTTPException):
        _autenticar(token)


@pytest.mark.usefixtures("mcp")
def test_token_mcp_de_conexao_que_escapou_da_revogacao_tambem_nao_vale(db_session, equipe):
    """Defesa em profundidade: a conexão fica ativa à força, e o token ainda
    não vale, porque a empresa dela não existe para a sessão."""
    token = _token_mcp(db_session, equipe, equipe.cliente)
    equipe.remover()
    db_session.execute(
        ConexaoMcp.__table__.update()
        .where(ConexaoMcp.empresa_id == equipe.cliente.id)
        .values(revogada_em=None)
    )
    db_session.execute(TokenOAuth.__table__.update().values(revogado_em=None))
    db_session.commit()

    with pytest.raises(HTTPException) as erro:
        _autenticar(token)

    assert erro.value.status_code == 401


# --- link público do relatório ---------------------------------------------------------


def test_link_publico_da_empresa_removida_responde_como_link_inexistente_e_volta_ao_restaurar(
    db_session, conta, criar_empresa  # noqa: F811
):
    c = conta()
    cliente = criar_empresa("Cliente com Fechamento", conta_id=c.conta_id)
    c.entrar(cliente.id)
    c.fechar_setembro()
    token = _criar(c.headers["admin"]).json()["token"]
    assert _consultar(token).status_code == 200
    sem_cabecalho = {"Authorization": c.headers["admin"]["Authorization"]}

    removida = client.delete(f"/empresas/{cliente.id}", headers=sem_cabecalho)
    assert removida.status_code == 200, removida.text

    resposta = _consultar(token)
    assert resposta.status_code == 404
    assert resposta.json() == INVALIDO
    assert _consultar("token-que-nunca-existiu").json() == INVALIDO

    client.post(f"/empresas/{cliente.id}/restaurar", headers=sem_cabecalho)
    assert _consultar(token).status_code == 200


# --- explicação automática -------------------------------------------------------------


class _ProvedorQueNaoPodeSerChamado:
    def __getattr__(self, nome):
        raise AssertionError(f"o provedor não pode ser chamado ({nome})")


def test_explicacao_automatica_nao_roda_para_empresa_removida(equipe, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(settings, "explicacao_automatica_habilitada", True)
    monkeypatch.setattr(settings, "llm_habilitado", True)
    equipe.remover()

    resumo = geracao.gerar_explicacoes_do_par(
        equipe.cliente.id, uuid.uuid4(), uuid.uuid4(), _ProvedorQueNaoPodeSerChamado()
    )

    assert (resumo.falhas, resumo.cache, resumo.teto) == (0, 0, 0)
    assert "explicacao_automatica resultado=empresa_removida" in caplog.text


# --- o filtro de sessão ----------------------------------------------------------------


def test_nenhuma_consulta_orm_enxerga_a_empresa_removida_a_menos_que_peca(db_session, equipe):
    equipe.remover()
    # Sessão sem a empresa carregada, como a de uma requisição: um objeto que a
    # sessão já tem na mão volta pelo `get` (identidade), e o recarregamento
    # de colunas dele não é filtrado, de propósito.
    db_session.expunge_all()
    removida = Empresa.id == equipe.cliente.id

    assert db_session.scalar(select(Empresa).where(removida)) is None
    assert db_session.get(Empresa, equipe.cliente.id) is None
    assert db_session.scalar(select(func.count()).where(removida)) == 0
    assert db_session.scalar(select(func.count()).select_from(Empresa).where(removida)) == 0
    assert db_session.scalar(select(Empresa.id).where(removida)) is None
    assert (
        db_session.scalar(
            select(Empresa.id).where(Empresa.id.in_(select(Empresa.id).where(removida)))
        )
        is None
    )
    assert db_session.scalar(select(func.count()).where(Empresa.conta_id == equipe.conta_id)) == 1
    assert (
        len(
            db_session.scalar(
                select(Empresa).where(Empresa.conta_id == equipe.conta_id)
            ).conta.empresas
        )
        == 1
    )
    # Pedindo, enxerga.
    for consulta in (
        select(Empresa).where(removida),
        select(func.count()).select_from(Empresa).where(removida),
    ):
        assert db_session.scalar(consulta.execution_options(incluir_removidas=True))
    assert db_session.get(Empresa, equipe.cliente.id, execution_options={"incluir_removidas": True})


def test_o_filtro_nao_atrapalha_a_linha_que_a_sessao_ja_carregou(db_session, equipe):
    carregada = db_session.scalar(select(Empresa).where(Empresa.id == equipe.cliente.id))
    equipe.remover()

    # A linha que a sessão já tinha na mão continua legível.
    db_session.refresh(carregada)
    assert carregada.razao_social == "Cliente Removida Ltda"
    assert carregada.removida_em is not None


def test_favorita_da_empresa_removida_volta_na_restauracao(db_session, equipe):
    client.put(f"/empresas/{equipe.cliente.id}/favorita", headers=equipe.headers)
    equipe.remover()
    assert client.get("/empresas", headers=equipe.headers).json()["itens"][0]["favorita"] is False

    equipe.restaurar()

    assert client.get("/empresas", headers=equipe.headers).json()["itens"][0]["favorita"] is True
    assert db_session.scalar(
        select(func.count())
        .select_from(EmpresaFavorita)
        .where(EmpresaFavorita.empresa_id == equipe.cliente.id)
    )


# --- consumo: remover não devolve franquia, uso nem limite do provedor -------------------


def _execucao(db_session, empresa, criar_extrato_da_empresa):
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    db_session.add(
        ExecucaoConciliacao(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            tolerancia_dias=0,
            total=1,
            match_exato=1,
            match_tolerancia=0,
            duplicado=0,
            sem_correspondencia=0,
            tarifa_bancaria=0,
            divergente_valor=0,
            divergente_data=0,
        )
    )
    db_session.commit()


def test_remover_a_empresa_nao_devolve_a_franquia_de_conciliacoes_do_mes(
    db_session, equipe, criar_extrato_da_empresa
):
    _execucao(db_session, equipe.cliente, criar_extrato_da_empresa)
    assert plano.conciliacoes_no_mes(db_session, equipe.conta_id) == 1

    equipe.remover()

    db_session.rollback()
    assert plano.conciliacoes_no_mes(db_session, equipe.conta_id) == 1
    me = client.get("/me", headers=equipe.headers).json()
    assert me["limites"]["conciliacoes"]["usadas"] == 1


def test_o_uso_do_painel_conta_a_conciliacao_da_empresa_removida_mas_nao_a_empresa(
    db_session, equipe, criar_extrato_da_empresa
):
    _execucao(db_session, equipe.cliente, criar_extrato_da_empresa)
    antes = admin._uso(db_session, equipe.conta_id, datetime.now(UTC))
    assert (antes.empresas, antes.conciliacoes_total) == (2, 1)

    equipe.remover()

    db_session.rollback()
    depois = admin._uso(db_session, equipe.conta_id, datetime.now(UTC))
    assert (depois.empresas, depois.conciliacoes_total) == (1, 1)
    assert depois.extratos_do_banco_por_mes == antes.extratos_do_banco_por_mes


def test_as_chaves_ativas_do_painel_batem_com_as_que_o_cliente_ve(db_session, equipe):
    """As chaves da empresa removida são revogadas na remoção: o `/admin` e a
    lista do cliente contam as mesmas ativas."""
    for nome in ("Da conta 1", "Da conta 2", "Da conta 3"):
        client.post("/chaves-api", headers=equipe.headers, json={"nome": nome})
    for nome in ("Da cliente 1", "Da cliente 2"):
        client.post(
            "/chaves-api",
            headers=equipe.headers,
            json={"nome": nome, "empresa_id": str(equipe.cliente.id)},
        )

    equipe.remover()

    db_session.rollback()
    itens = client.get("/chaves-api", headers=equipe.headers).json()["itens"]
    ativas_na_lista = [i for i in itens if i["revogada_em"] is None]
    uso = admin._uso(db_session, equipe.conta_id, datetime.now(UTC))
    assert len(ativas_na_lista) == uso.chaves_api_ativas == 3


def test_remover_a_empresa_nao_zera_o_limite_diario_de_explicacoes_da_conta(
    db_session, equipe, monkeypatch
):
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 1)
    monkeypatch.setattr(settings, "llm_limite_diario_global", 100_000)
    db_session.add(
        ExplicacaoDivergencia(
            empresa_id=equipe.cliente.id,
            chave=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            status="gerada",
            provedor="falso",
            modelo="falso",
            versao_prompt="1",
            texto="Explicação de teste.",
            tokens_entrada=1,
            tokens_saida=1,
        )
    )
    db_session.commit()
    assert geracao._limite_atingido(db_session, equipe.principal.id) is True

    equipe.remover()

    db_session.rollback()
    assert geracao._limite_atingido(db_session, equipe.principal.id) is True
