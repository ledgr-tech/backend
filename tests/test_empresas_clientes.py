"""Empresas clientes da conta (issue #133, parte 2, ADR-019): `GET` e
`POST /empresas`.

Rotas da conta: ignoram `X-Ledgr-Empresa`. Todos os papéis listam; só admin e
gestor cadastram. O 402 do limite de empresas do plano está em
tests/test_limites_plano.py. A BrasilAPI é a falsa de
tests/test_consulta_cnpj_cadastro.py: nada sai para a internet.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import _gerar_cnpj, _gerar_cnpj_alfanumerico, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_consulta_cnpj import NAO_ENCONTRADO_BRASILAPI
from test_consulta_cnpj_cadastro import BASE, ReceitaFalsa

from app.api.empresas import LIMITE_EMPRESAS_POR_HORA
from app.core.auth import CABECALHO_EMPRESA
from app.models import Configuracao, Empresa
from app.services.consulta_cnpj import ConsultaBrasilAPI, obter_consulta_cnpj_dependencia
from main import app

client = TestClient(app)


@pytest.fixture
def receita():
    falsa = ReceitaFalsa()
    consulta = ConsultaBrasilAPI(
        base_url=BASE, cliente_http=httpx.Client(transport=httpx.MockTransport(falsa.handler))
    )
    app.dependency_overrides[obter_consulta_cnpj_dependencia] = lambda: consulta
    yield falsa
    app.dependency_overrides.pop(obter_consulta_cnpj_dependencia, None)


@pytest.fixture
def conta(db_session, criar_usuario, gerar_token):
    """Admin, gestor e analista na mesma conta, e os cabeçalhos de cada um."""
    admin = criar_usuario(nome="Ana Admin")
    usuarios = {
        "admin": admin,
        "gestor": criar_usuario(conta_id=admin.conta_id, papel="gestor"),
        "analista": criar_usuario(conta_id=admin.conta_id, papel="analista"),
    }
    headers = {
        papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id)) for papel, u in usuarios.items()
    }
    return admin.conta_id, headers


def _cadastrar(headers, cnpj=None, razao_social="Cliente Novo Ltda"):
    return client.post(
        "/empresas",
        headers=headers,
        json={"cnpj": cnpj or _gerar_cnpj(), "razao_social": razao_social},
    )


# --- GET /empresas ------------------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_todos_os_papeis_listam_as_empresas_da_conta_em_ordem(
    db_session, conta, criar_empresa, papel
):
    conta_id, headers = conta
    zeta = criar_empresa("Zeta", conta_id=conta_id)
    alfa_1 = criar_empresa("Alfa", conta_id=conta_id)
    alfa_2 = criar_empresa("Alfa", conta_id=conta_id)
    criar_empresa("Outra conta")
    primeira = db_session.scalars(
        select(Empresa).where(
            Empresa.conta_id == conta_id, Empresa.razao_social == "Empresa de teste"
        )
    ).one()

    resposta = client.get("/empresas", headers={**headers[papel], CABECALHO_EMPRESA: "x"})

    assert resposta.status_code == 200
    itens = resposta.json()["itens"]
    alfas = sorted([alfa_1, alfa_2], key=lambda e: e.id)
    assert [item["id"] for item in itens] == [str(e.id) for e in [*alfas, primeira, zeta]]
    assert set(itens[0]) == {"id", "razao_social", "cnpj", "cnpj_verificado_em", "criado_em"}
    assert itens[0]["cnpj"] == alfas[0].cnpj
    assert itens[0]["cnpj_verificado_em"] is None
    assert datetime.fromisoformat(itens[0]["criado_em"]).tzinfo is not None


def test_listar_sem_token_e_401():
    assert client.get("/empresas").status_code == 401


# --- POST /empresas: papéis ---------------------------------------------------------


@pytest.mark.parametrize(
    ("papel", "esperado"), [("admin", 201), ("gestor", 201), ("analista", 403)]
)
def test_quem_cadastra_empresa(db_session, conta, papel, esperado):
    conta_id, headers = conta
    cnpj = _gerar_cnpj()

    resposta = _cadastrar(headers[papel], cnpj)

    assert resposta.status_code == esperado, resposta.text
    criada = db_session.scalar(
        select(Empresa).where(Empresa.conta_id == conta_id, Empresa.cnpj == cnpj)
    )
    if esperado == 403:
        assert resposta.json() == {"detail": "Só o admin ou o gestor da conta podem fazer isso."}
        assert criada is None
        return
    corpo = resposta.json()
    assert corpo["id"] == str(criada.id)
    assert (corpo["cnpj"], corpo["razao_social"]) == (cnpj, "Cliente Novo Ltda")
    assert corpo["cnpj_verificado_em"] is None  # consulta desligada
    configuracao = db_session.scalar(
        select(Configuracao).where(Configuracao.empresa_id == criada.id)
    )
    assert configuracao.tolerancia_dias_default == 0


def test_cnpj_com_mascara_e_alfanumerico_minusculo_sao_normalizados(db_session, conta):
    _, headers = conta
    numerico, alfanumerico = _gerar_cnpj(), _gerar_cnpj_alfanumerico()
    mascara = f"{numerico[:2]}.{numerico[2:5]}.{numerico[5:8]}/{numerico[8:12]}-{numerico[12:]}"

    com_mascara = _cadastrar(headers["admin"], mascara)
    minusculo = _cadastrar(headers["admin"], alfanumerico.lower())

    assert com_mascara.json()["cnpj"] == numerico
    assert minusculo.json()["cnpj"] == alfanumerico


@pytest.mark.parametrize(
    "corpo",
    [
        {"cnpj": "11.222.333/0001-80", "razao_social": "Cliente"},
        {"cnpj": "11222333000181", "razao_social": "   "},
        {"cnpj": "11222333000181"},
    ],
    ids=["cnpj_invalido", "razao_vazia", "sem_razao"],
)
def test_corpo_invalido_e_422(db_session, conta, corpo):
    _, headers = conta

    assert client.post("/empresas", headers=headers["admin"], json=corpo).status_code == 422


# --- CNPJ repetido e acesso cruzado -------------------------------------------------


def test_cnpj_repetido_na_mesma_conta_e_409_e_em_outra_conta_e_201(
    db_session, conta, criar_usuario, gerar_token
):
    _, headers = conta
    cnpj = _gerar_cnpj()
    assert _cadastrar(headers["admin"], cnpj).status_code == 201

    repetido = _cadastrar(headers["gestor"], cnpj)
    outra = criar_usuario()
    na_outra_conta = _cadastrar(cabecalhos(gerar_token(outra.conta_id, usuario_id=outra.id)), cnpj)

    assert repetido.status_code == 409
    assert repetido.json() == {"detail": "Empresa já cadastrada nesta conta."}
    assert na_outra_conta.status_code == 201


def test_o_cnpj_da_propria_conta_ja_e_a_primeira_empresa(db_session, criar_usuario, gerar_token):
    admin = criar_usuario()
    headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id))

    assert _cadastrar(headers, admin.conta.cnpj).status_code == 409


def test_empresa_criada_na_conta_a_nao_existe_para_a_conta_b(
    db_session, conta, criar_usuario, gerar_token
):
    _, headers = conta
    nova = _cadastrar(headers["admin"]).json()["id"]
    outra = criar_usuario()
    headers_b = cabecalhos(gerar_token(outra.conta_id, usuario_id=outra.id))

    lista_b = client.get("/empresas", headers=headers_b).json()["itens"]
    como_ativa_na_b = client.get("/extratos", headers={**headers_b, CABECALHO_EMPRESA: nova})
    como_ativa_na_a = client.get(
        "/extratos", headers={**headers["analista"], CABECALHO_EMPRESA: nova}
    )

    assert nova not in {item["id"] for item in lista_b}
    assert como_ativa_na_b.status_code == 404
    assert como_ativa_na_b.json() == {"detail": "Empresa não encontrada."}
    assert como_ativa_na_a.status_code == 200
    # Com duas empresas, a conta A passa a precisar do cabeçalho.
    assert client.get("/extratos", headers=headers["analista"]).status_code == 400


# --- trava de 20 empresas por hora --------------------------------------------------


def _empresas_antigas(db_session, conta_id, quantidade, ha=timedelta(0)):
    """Empresas da conta gravadas direto, com `criado_em` `ha` tempo atrás."""
    ids = []
    for _ in range(quantidade):
        empresa = Empresa(conta_id=conta_id, razao_social="Cliente em lote", cnpj=_gerar_cnpj())
        db_session.add(empresa)
        db_session.flush()
        ids.append(empresa.id)
    if ha:
        db_session.execute(
            update(Empresa)
            .where(Empresa.id.in_(ids))
            .values(criado_em=datetime.now(UTC).replace(tzinfo=None) - ha)
        )
    db_session.commit()


def test_mais_de_20_empresas_na_ultima_hora_e_429(db_session, conta):
    conta_id, headers = conta
    # A primeira empresa, do cadastro, conta: com mais 19, a janela fica cheia.
    _empresas_antigas(db_session, conta_id, LIMITE_EMPRESAS_POR_HORA - 1)

    resposta = _cadastrar(headers["admin"])

    assert resposta.status_code == 429
    assert 1 <= int(resposta.headers["Retry-After"]) <= 3600
    assert resposta.json()["detail"].startswith("Limite de empresas cadastradas por hora")


def test_empresas_de_mais_de_uma_hora_nao_contam(db_session, conta):
    conta_id, headers = conta
    _empresas_antigas(db_session, conta_id, LIMITE_EMPRESAS_POR_HORA, ha=timedelta(hours=2))

    assert _cadastrar(headers["gestor"]).status_code == 201


def test_a_trava_por_hora_e_por_conta(db_session, conta, criar_usuario, gerar_token):
    conta_id, _ = conta
    _empresas_antigas(db_session, conta_id, LIMITE_EMPRESAS_POR_HORA)
    outra = criar_usuario()

    resposta = _cadastrar(cabecalhos(gerar_token(outra.conta_id, usuario_id=outra.id)))

    assert resposta.status_code == 201


# --- consulta na Receita ------------------------------------------------------------


def test_receita_ativa_marca_a_empresa_como_verificada(db_session, conta, receita):
    _, headers = conta
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj)
    db_session.rollback()  # devolve ao pool a conexão da sessão do teste

    resposta = _cadastrar(headers["admin"], cnpj)

    assert resposta.status_code == 201
    assert resposta.json()["cnpj_verificado_em"] is not None
    assert len(receita.pedidos) == 1
    # A chamada externa roda sem conexão do banco em uso pela rota.
    assert receita.conexoes_em_uso == [0]


@pytest.mark.parametrize(
    ("montar", "mensagem"),
    [
        (
            lambda receita, cnpj: setattr(
                receita, "resposta", httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI)
            ),
            "CNPJ não encontrado na Receita Federal.",
        ),
        (
            lambda receita, cnpj: receita.responder_ativa(cnpj, situacao="BAIXADA"),
            "CNPJ não está ativo na Receita Federal.",
        ),
    ],
    ids=["nao_encontrado", "inativa"],
)
def test_receita_recusando_e_422_e_nada_e_gravado(db_session, conta, receita, montar, mensagem):
    conta_id, headers = conta
    cnpj = _gerar_cnpj()
    montar(receita, cnpj)

    resposta = _cadastrar(headers["admin"], cnpj)

    assert resposta.status_code == 422
    (erro,) = resposta.json()["detail"]
    assert (erro["loc"], erro["msg"]) == (["body", "cnpj"], mensagem)
    assert (
        db_session.scalar(
            select(Empresa.id).where(Empresa.conta_id == conta_id, Empresa.cnpj == cnpj)
        )
        is None
    )


def test_receita_fora_do_ar_nao_bloqueia(db_session, conta, receita):
    _, headers = conta
    receita.resposta = httpx.ConnectError

    resposta = _cadastrar(headers["gestor"])

    assert resposta.status_code == 201
    assert resposta.json()["cnpj_verificado_em"] is None


def test_cnpj_repetido_nao_consulta_a_receita(db_session, conta, receita):
    conta_id, headers = conta
    cnpj_da_conta = db_session.scalars(
        select(Empresa.cnpj).where(Empresa.conta_id == conta_id)
    ).one()

    assert _cadastrar(headers["admin"], cnpj_da_conta).status_code == 409
    assert receita.pedidos == []


def test_cadastro_sem_token_e_401():
    assert (
        client.post("/empresas", json={"cnpj": "11222333000181", "razao_social": "X"}).status_code
        == 401
    )


def test_id_novo_e_uuid(db_session, conta):
    _, headers = conta

    assert uuid.UUID(_cadastrar(headers["admin"]).json()["id"])
