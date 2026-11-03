"""Painel administrativo interno (issue #156, ADR-025): operador, registro de
acesso, contas, uso, inativação e o efeito da conta inativa."""

import hashlib
import io
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import SENHA_USUARIO_TESTE, cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app.core import conta_inativa
from app.core.config import settings
from app.models import (
    AcessoAdmin,
    Conta,
    Convite,
    Empresa,
    EventoConta,
    ExecucaoConciliacao,
    OperadorLedgr,
    Usuario,
)
from app.services.ia import geracao
from main import app

client = TestClient(app)

SO_OPERADOR = {"detail": "Acesso restrito à equipe do Ledgr."}
LOGIN_ANTIGO = {"detail": "Entre de novo para usar o painel."}
INATIVA = conta_inativa.corpo()
OFX = (__import__("pathlib").Path(__file__).parent / "fixtures" / "extrato_valido.ofx").read_bytes()


def _agora_em_segundos() -> int:
    return int(datetime.now(UTC).timestamp())


@pytest.fixture
def operador(db_session, criar_usuario, gerar_token):
    """`operador()` cria um usuário numa conta própria (a do Ledgr) e o inclui
    como operador; `auth_time` muda a idade do login."""

    def _novo(nome: str = "Olga Operadora", auth_time: int | None = None):
        usuario = criar_usuario(nome=nome)
        registro = OperadorLedgr(usuario_id=usuario.id, incluido_por="teste")
        db_session.add(registro)
        db_session.commit()
        extra = {} if auth_time is None else {"auth_time": auth_time}
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, **extra)
        return SimpleNamespace(usuario=usuario, registro=registro, headers=cabecalhos(token))

    return _novo


@pytest.fixture
def cliente(db_session, criar_usuario, criar_empresa, gerar_token):
    """`cliente()` cria uma conta cliente com admin, gestor e analista, e uma
    empresa cliente a mais com dado que o painel nunca pode mostrar."""

    def _nova(razao_social: str = "Escritório Cliente Ltda", email: str | None = None):
        admin_da_conta = criar_usuario(nome="Ana Admin")
        conta_id = admin_da_conta.conta_id
        db_session.execute(
            update(Conta).where(Conta.id == conta_id).values(razao_social=razao_social)
        )
        if email:
            admin_da_conta.email = email
        db_session.commit()
        usuarios = {
            "admin": admin_da_conta,
            "gestor": criar_usuario(conta_id=conta_id, nome="Gil Gestor", papel="gestor"),
            "analista": criar_usuario(conta_id=conta_id, nome="Ada Analista", papel="analista"),
        }
        empresa_id = empresa_unica(db_session, conta_id)
        criar_empresa("Padaria Segredo do Cliente", conta_id=conta_id)
        headers = {
            papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id), empresa_id)
            for papel, u in usuarios.items()
        }
        return SimpleNamespace(
            conta_id=conta_id, usuarios=usuarios, empresa_id=empresa_id, headers=headers
        )

    return _nova


def _contas(headers, **params):
    return client.get("/admin/contas", headers=headers, params=params)


def _detalhe(headers, conta_id):
    return client.get(f"/admin/contas/{conta_id}", headers=headers)


def _acao(headers, conta_id, acao, **corpo):
    return client.post(f"/admin/contas/{conta_id}/{acao}", headers=headers, json=corpo)


def _acessos(db_session) -> list[AcessoAdmin]:
    db_session.expire_all()
    return db_session.scalars(select(AcessoAdmin).order_by(AcessoAdmin.em)).all()


@pytest.fixture(autouse=True)
def _sem_acessos_de_outros_testes(db_session):
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()


# --- quem entra -----------------------------------------------------------------------


ROTAS = [
    ("GET", "/admin/contas", None),
    ("GET", "/admin/contas/{conta_id}", None),
    ("POST", "/admin/contas/{conta_id}/inativar", {"motivo": "x"}),
    ("POST", "/admin/contas/{conta_id}/reativar", {"motivo": "x"}),
    ("POST", "/admin/contas/{conta_id}/piloto", {"ativo": True, "motivo": "x"}),
]


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_quem_nao_e_operador_recebe_403_e_fica_registrado(db_session, cliente, papel):
    c = cliente()

    for metodo, rota, corpo in ROTAS:
        resposta = client.request(
            metodo, rota.format(conta_id=c.conta_id), headers=c.headers[papel], json=corpo
        )
        assert (resposta.status_code, resposta.json()) == (403, SO_OPERADOR), rota

    acessos = _acessos(db_session)
    assert [(a.metodo, a.rota, a.resultado) for a in acessos] == [
        (metodo, rota, "nao_operador") for metodo, rota, _ in ROTAS
    ]
    assert {a.usuario_id for a in acessos} == {c.usuarios[papel].id}
    assert {a.operador_id for a in acessos} == {None}
    db_session.expire_all()
    assert db_session.get(Conta, c.conta_id).inativada_em is None


def test_sem_login_e_401_sem_registro(db_session):
    assert client.get("/admin/contas").status_code == 401
    assert _acessos(db_session) == []


def test_login_antigo_e_401_e_fica_registrado(db_session, operador, cliente):
    c = cliente()
    antigo = operador(auth_time=_agora_em_segundos() - 2 * 3600 - 60)
    recente = operador(nome="Rui Recente", auth_time=_agora_em_segundos() - 2 * 3600 + 60)

    negado = _detalhe(antigo.headers, c.conta_id)
    permitido = _detalhe(recente.headers, c.conta_id)

    assert (negado.status_code, negado.json()) == (401, LOGIN_ANTIGO)
    assert permitido.status_code == 200
    assert [(a.resultado, a.operador_id) for a in _acessos(db_session)] == [
        ("login_antigo", antigo.registro.id),
        ("permitido", recente.registro.id),
    ]


def test_operador_removido_perde_o_acesso_na_hora(db_session, operador):
    o = operador()
    assert _contas(o.headers).status_code == 200

    db_session.execute(
        update(OperadorLedgr)
        .where(OperadorLedgr.id == o.registro.id)
        .values(removido_em=func.now(), removido_por="teste")
    )
    db_session.commit()

    assert _contas(o.headers).json() == SO_OPERADOR


def test_admin_ignora_a_empresa_ativa_e_a_conta_inativa_do_operador(db_session, operador):
    o = operador()
    db_session.execute(
        update(Conta)
        .where(Conta.id == o.usuario.conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()

    lista = _contas({**o.headers, "X-Ledgr-Empresa": str(uuid.uuid4())})
    alvo = lista.json()["itens"][0]["id"]
    acao = _acao(o.headers, alvo, "piloto", ativo=True, motivo="Piloto")

    assert lista.status_code == 200
    assert acao.status_code in (200, 409)


# --- registro de acesso ---------------------------------------------------------------


def test_registro_tem_rota_e_conta_consultada_sem_o_termo_buscado(db_session, operador, cliente):
    o = operador()
    c = cliente(email="contato.secreto@cliente.com.br")

    _contas(o.headers, busca="contato.secreto@cliente.com.br")
    _detalhe(o.headers, c.conta_id)

    acessos = _acessos(db_session)
    assert [(a.metodo, a.rota, a.conta_consultada_id, a.resultado) for a in acessos] == [
        ("GET", "/admin/contas", None, "permitido"),
        ("GET", "/admin/contas/{conta_id}", c.conta_id, "permitido"),
    ]
    bruto = db_session.execute(
        text("SELECT string_agg(row_to_json(a)::text, '') FROM acessos_admin a")
    ).scalar_one()
    assert "contato.secreto" not in bruto


def test_retencao_de_um_ano(db_session, operador):
    o = operador()
    for dias, resultado in ((366, "permitido"), (364, "nao_operador")):
        db_session.add(
            AcessoAdmin(
                usuario_id=o.usuario.id,
                metodo="GET",
                rota="/admin/contas",
                resultado=resultado,
                em=datetime.now(UTC) - timedelta(days=dias),
            )
        )
    db_session.commit()

    _contas(o.headers)

    idades = sorted((datetime.now(UTC) - a.em).days for a in _acessos(db_session))
    assert idades == [0, 364]


# --- lista de contas ------------------------------------------------------------------


def _estado(db_session, conta_id, **valores):
    db_session.execute(update(Conta).where(Conta.id == conta_id).values(**valores))
    db_session.commit()


def test_busca_por_razao_social_cnpj_e_email_do_admin(db_session, operador, cliente):
    o = operador()
    alvo = cliente("Contabilidade São João Ltda", email="Joana.Dono@Escritorio.com.br")
    cliente("Outra Assessoria")
    cnpj = db_session.get(Conta, alvo.conta_id).cnpj
    com_mascara = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"

    for termo in ("sao joao", "SÃO JOÃO", com_mascara, cnpj[:8], "joana.dono", "ESCRITORIO.COM"):
        ids = [item["id"] for item in _contas(o.headers, busca=termo).json()["itens"]]
        assert ids == [str(alvo.conta_id)], termo
    assert _contas(o.headers, busca="%").json()["total"] == 0


def test_filtros_por_situacao_separam_a_inativa(db_session, operador, cliente):
    o = operador()
    agora = datetime.now(UTC)
    contas = {
        "sem_plano": cliente("Sem Plano"),
        "teste": cliente("No Teste"),
        "teste_encerrado": cliente("Teste Acabou"),
        "ativo": cliente("Piloto"),
        "inativa": cliente("Inativada"),
    }
    _estado(
        db_session,
        contas["teste"].conta_id,
        plano="essencial",
        teste_termina_em=agora + timedelta(days=3),
    )
    _estado(
        db_session,
        contas["teste_encerrado"].conta_id,
        plano="padrao",
        teste_termina_em=agora - timedelta(days=1),
    )
    _estado(db_session, contas["ativo"].conta_id, assinatura_ativa=True)
    _estado(
        db_session,
        contas["inativa"].conta_id,
        plano="essencial",
        teste_termina_em=agora + timedelta(days=3),
        inativada_em=agora,
        motivo_inativacao="teste",
    )
    criadas = {str(c.conta_id) for c in contas.values()}

    for situacao, c in contas.items():
        itens = _contas(o.headers, situacao=situacao, limit=100).json()["itens"]
        nossas = [item for item in itens if item["id"] in criadas]
        assert [item["id"] for item in nossas] == [str(c.conta_id)], situacao
    (inativa,) = [
        i for i in _contas(o.headers, situacao="inativa").json()["itens"] if i["id"] in criadas
    ]
    # A situação comercial continua a dela; a inativação é à parte.
    assert (inativa["situacao"], inativa["inativa"]) == ("teste", True)
    assert _contas(o.headers, situacao="cancelada").status_code == 422


def test_ordem_e_paginacao(db_session, operador, cliente):
    o = operador()
    agora = datetime.now(UTC)
    primeira, segunda, terceira = cliente("Primeira"), cliente("Segunda"), cliente("Terceira")
    _estado(
        db_session, primeira.conta_id, plano="essencial", teste_termina_em=agora + timedelta(days=9)
    )
    _estado(
        db_session, terceira.conta_id, plano="essencial", teste_termina_em=agora + timedelta(days=1)
    )
    nossas = {str(c.conta_id) for c in (primeira, segunda, terceira)}

    def ordem(**params):
        itens = _contas(o.headers, limit=100, **params).json()["itens"]
        return [item["id"] for item in itens if item["id"] in nossas]

    assert ordem() == [str(terceira.conta_id), str(segunda.conta_id), str(primeira.conta_id)]
    assert ordem(ordem="teste_termina_em")[:2] == [str(terceira.conta_id), str(primeira.conta_id)]
    assert ordem(ordem="teste_termina_em")[-1] == str(segunda.conta_id)
    pagina = _contas(o.headers, limit=2, offset=1).json()
    total = _contas(o.headers, limit=100).json()["total"]
    assert (pagina["total"], pagina["limit"], pagina["offset"], len(pagina["itens"])) == (
        total,
        2,
        1,
        min(2, total - 1),
    )
    assert _contas(o.headers, limit=0).status_code == 422
    assert _contas(o.headers, limit=101).status_code == 422


# --- detalhe e uso --------------------------------------------------------------------


def _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, quando):
    banco = criar_extrato_da_empresa(c.empresa_id, "banco")
    sistema = criar_extrato_da_empresa(c.empresa_id, "sistema")
    inserir_lancamentos(banco, [("10.00", "Pix para Maria Sigilosa")])
    inserir_lancamentos(sistema, [("10.00", "Pix para Maria Sigilosa")])
    for extrato in (banco, sistema):
        extrato.nome_arquivo = "arquivo-secreto-do-cliente.ofx"
        extrato.periodo_inicio = extrato.periodo_fim = date(2026, 9, 5)
    db_session.commit()
    for _ in range(2):  # reconciliar não conta de novo
        resposta = client.post(
            "/conciliacoes",
            headers=c.headers["admin"],
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )
        assert resposta.status_code == 201, resposta.text
    db_session.execute(
        update(ExecucaoConciliacao)
        .where(ExecucaoConciliacao.extrato_banco_id == banco.id)
        .values(criado_em=quando)
    )
    db_session.commit()


def _mes(agora: datetime, atras: int) -> tuple[str, datetime]:
    ano, mes = agora.year, agora.month - atras
    while mes < 1:
        ano, mes = ano - 1, mes + 12
    return f"{ano:04d}-{mes:02d}", datetime(ano, mes, 10, 12, tzinfo=UTC)


def test_detalhe_com_o_contato_e_o_uso(
    db_session, operador, cliente, criar_extrato_da_empresa, inserir_lancamentos
):
    o = operador()
    c = cliente("Escritório Uso Ltda", email="contato.uso@escritorio.com.br")
    # O mês é o civil de São Paulo, o da franquia (issues #166 e #167).
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    mes_atual, neste_mes = _mes(agora, 0)
    mes_passado, no_mes_passado = _mes(agora, 1)
    _, ha_quatro_meses = _mes(agora, 4)
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, neste_mes)
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, neste_mes)
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, no_mes_passado)
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, ha_quatro_meses)
    assert (
        client.post("/chaves-api", headers=c.headers["admin"], json={"nome": "ERP"}).status_code
        == 201
    )
    _convite(db_session, c, "novo@cliente.com.br")
    login = client.post(
        "/login", json={"email": c.usuarios["gestor"].email, "senha": SENHA_USUARIO_TESTE}
    )
    assert login.status_code == 200

    corpo = _detalhe(o.headers, c.conta_id).json()

    assert corpo["admin"] == {"nome": "Ana Admin", "email": "contato.uso@escritorio.com.br"}
    assert (corpo["razao_social"], corpo["situacao"], corpo["inativa"]) == (
        "Escritório Uso Ltda",
        "sem_plano",
        False,
    )
    assert corpo["inativacao"] is None
    assert corpo["ultimo_login_em"] is not None
    uso = corpo["uso"]
    assert (uso["empresas"], uso["usuarios_ativos"], uso["convites_pendentes"]) == (2, 3, 1)
    assert uso["conciliacoes_total"] == 8
    assert uso["extratos_do_banco_por_mes"][:2] == [
        {"mes": mes_atual, "extratos_do_banco": 2},
        {"mes": mes_passado, "extratos_do_banco": 1},
    ]
    assert len(uso["extratos_do_banco_por_mes"]) == 3
    assert (uso["chaves_api_ativas"], uso["conexoes_mcp_ativas"], uso["links_publicos_ativos"]) == (
        1,
        0,
        0,
    )


def test_nenhum_dado_do_cliente_no_painel(
    db_session, operador, cliente, criar_extrato_da_empresa, inserir_lancamentos
):
    o = operador()
    c = cliente()
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, datetime.now(UTC))

    detalhe = _detalhe(o.headers, c.conta_id)
    lista = _contas(o.headers, limit=100)

    for resposta in (detalhe, lista):
        assert resposta.status_code == 200
        for proibido in ("Maria Sigilosa", "arquivo-secreto", "Padaria Segredo", "Gil Gestor"):
            assert proibido not in resposta.text
    assert set(detalhe.json()) == {
        "id",
        "razao_social",
        "cnpj",
        "plano",
        "situacao",
        "teste_termina_em",
        "assinatura_ativa",
        "inativa",
        "criada_em",
        "admin",
        "inativacao",
        "ultimo_login_em",
        "uso",
        "eventos",
    }
    assert set(lista.json()["itens"][0]) == {
        "id",
        "razao_social",
        "cnpj",
        "plano",
        "situacao",
        "teste_termina_em",
        "assinatura_ativa",
        "inativa",
        "criada_em",
    }
    assert _detalhe(o.headers, uuid.uuid4()).json() == {"detail": "Conta não encontrada."}


# --- ações ----------------------------------------------------------------------------


def test_inativar_e_reativar_com_evento_e_409_na_repeticao(db_session, operador, cliente):
    o = operador()
    c = cliente()

    inativou = _acao(o.headers, c.conta_id, "inativar", motivo="  Pedido do cliente  ")
    de_novo = _acao(o.headers, c.conta_id, "inativar", motivo="De novo")
    reativou = _acao(o.headers, c.conta_id, "reativar", motivo="Voltou")
    reativar_de_novo = _acao(o.headers, c.conta_id, "reativar", motivo="De novo")

    assert inativou.status_code == 200
    assert inativou.json()["inativa"] is True
    assert inativou.json()["inativacao"]["motivo"] == "Pedido do cliente"
    assert (de_novo.status_code, de_novo.json()) == (409, {"detail": "A conta já está inativa."})
    assert reativou.json()["inativa"] is False and reativou.json()["inativacao"] is None
    assert reativar_de_novo.json() == {"detail": "A conta não está inativa."}
    assert [
        (e["tipo"], e["motivo"], e["operador"], e["origem"]) for e in reativou.json()["eventos"]
    ] == [
        ("reativada", "Voltou", "Olga Operadora", "painel"),
        ("inativada", "Pedido do cliente", "Olga Operadora", "painel"),
    ]
    for motivo in ("", "   ", "x" * 501):
        assert _acao(o.headers, c.conta_id, "inativar", motivo=motivo).status_code == 422
    assert _acao(o.headers, c.conta_id, "inativar").status_code == 422


def test_piloto_ativo(db_session, operador, cliente):
    o = operador()
    c = cliente()

    ligou = _acao(o.headers, c.conta_id, "piloto", ativo=True, motivo="Piloto de novembro")
    repetiu = _acao(o.headers, c.conta_id, "piloto", ativo=True, motivo="De novo")
    desligou = _acao(o.headers, c.conta_id, "piloto", ativo=False, motivo="Fim do piloto")

    assert (ligou.json()["situacao"], ligou.json()["assinatura_ativa"]) == ("ativo", True)
    assert repetiu.json() == {"detail": "O piloto já está ligado nesta conta."}
    assert desligou.json()["situacao"] == "sem_plano"
    assert [e["tipo"] for e in desligou.json()["eventos"]] == ["piloto_desligado", "piloto_ligado"]
    assert client.get("/me", headers=c.headers["admin"]).json()["situacao"] == "sem_plano"


# --- efeito da conta inativa ----------------------------------------------------------


def _inativar(db_session, conta_id):
    _estado(db_session, conta_id, inativada_em=func.now(), motivo_inativacao="teste")


def _reativar(db_session, conta_id):
    _estado(db_session, conta_id, inativada_em=None, motivo_inativacao=None)


def test_conta_inativa_le_e_exporta(
    db_session, cliente, criar_extrato_da_empresa, inserir_lancamentos
):
    c = cliente()
    _conciliar(c, criar_extrato_da_empresa, inserir_lancamentos, db_session, datetime.now(UTC))
    banco = db_session.scalar(
        select(ExecucaoConciliacao.extrato_banco_id).where(
            ExecucaoConciliacao.empresa_id == c.empresa_id
        )
    )
    _inativar(db_session, c.conta_id)

    for caminho in (
        "/me",
        "/extratos",
        f"/extratos/{banco}",
        "/execucoes",
        f"/conciliacoes/{banco}",
        f"/conciliacoes/{banco}/exportar",
        "/fechamentos",
        "/empresas",
        "/empresa/configuracoes",
        "/empresa/usuarios",
        "/chaves-api",
    ):
        assert client.get(caminho, headers=c.headers["admin"]).status_code == 200, caminho
    login = client.post(
        "/login", json={"email": c.usuarios["gestor"].email, "senha": SENHA_USUARIO_TESTE}
    )
    assert login.status_code == 200


ESCRITAS_BLOQUEADAS = [
    (
        "POST",
        "/conciliacoes",
        {"extrato_banco_id": str(uuid.uuid4()), "extrato_sistema_id": str(uuid.uuid4())},
    ),
    ("POST", "/conciliacoes/{u}/decisoes", {"chave": "x", "tipo": "conferida"}),
    ("POST", "/justificativas/lote", {"acao": "aprovar", "competencia": "2026-09"}),
    ("POST", "/explicacoes", {"conciliacao_id": str(uuid.uuid4())}),
    ("POST", "/assistente/perguntas", {"pergunta": "Como concilio?"}),
    ("POST", "/fechamentos", {"competencia": "2026-09", "ressalva": "x"}),
    ("DELETE", "/fechamentos/2026-09", None),
    ("POST", "/fechamentos/2026-09/links", {"validade_dias": 30}),
    ("POST", "/empresas", {"cnpj": "11222333000181", "razao_social": "Nova"}),
    ("PUT", "/empresa/configuracoes", {"tolerancia_dias": 1}),
    ("POST", "/empresa/plano", {"plano": "essencial"}),
    ("POST", "/empresa/convites", {"email": "x@y.com.br"}),
    ("DELETE", "/empresa/convites/{u}", None),
    ("PATCH", "/empresa/usuarios/{gestor}", {"papel": "analista"}),
    ("POST", "/empresa/admin/transferencia", {"usuario_id": "{gestor}", "senha_atual": "x"}),
    ("POST", "/chaves-api", {"nome": "ERP"}),
    ("POST", "/conexoes-mcp/pedidos/{u}/aprovar", {"empresa_id": "{u}"}),
    ("POST", "/conexoes-mcp/pedidos/{u}/recusar", None),
    ("DELETE", "/importacoes-ia/{u}", None),
    ("PATCH", "/importacoes-ia/{u}/lancamentos/{u}", {"valor": "1.00"}),
    ("POST", "/importacoes-ia/{u}/confirmar", None),
]


def _formatar(valor, c):
    if isinstance(valor, dict):
        return {k: _formatar(v, c) for k, v in valor.items()}
    if isinstance(valor, str):
        return valor.replace("{u}", str(uuid.uuid4())).replace(
            "{gestor}", str(c.usuarios["gestor"].id)
        )
    return valor


@pytest.mark.parametrize(
    ("metodo", "caminho", "corpo"),
    ESCRITAS_BLOQUEADAS,
    ids=[f"{m} {p}" for m, p, _ in ESCRITAS_BLOQUEADAS],
)
def test_conta_inativa_bloqueia_as_escritas(
    db_session, cliente, monkeypatch, metodo, caminho, corpo
):
    monkeypatch.setattr(settings, "mcp_habilitado", True)
    monkeypatch.setattr(settings, "mcp_url_publica", "https://api.ledgr.teste/mcp")
    monkeypatch.setattr(settings, "oauth_issuer", "https://api.ledgr.teste")
    monkeypatch.setattr(settings, "link_publico_habilitado", True)
    c = cliente()
    _inativar(db_session, c.conta_id)

    resposta = client.request(
        metodo, _formatar(caminho, c), headers=c.headers["admin"], json=_formatar(corpo, c)
    )

    assert (resposta.status_code, resposta.json()) == (403, INATIVA)


@pytest.mark.parametrize(
    "envio",
    [
        ("/extratos/upload", {"origem": "banco"}, ("banco.ofx", OFX)),
        ("/importacoes-ia", None, ("r.json", b'{"versao": "1", "lancamentos": []}')),
    ],
    ids=["upload", "importacao_ia"],
)
def test_conta_inativa_bloqueia_os_envios(db_session, cliente, envio):
    c = cliente()
    _inativar(db_session, c.conta_id)
    caminho, dados, (nome, conteudo) = envio

    resposta = client.post(
        caminho,
        headers=c.headers["admin"],
        data=dados,
        files={"arquivo": (nome, io.BytesIO(conteudo), "application/octet-stream")},
    )

    assert (resposta.status_code, resposta.json()) == (403, INATIVA)


def test_conta_inativa_libera_o_que_e_da_pessoa_e_as_revogacoes(db_session, cliente, monkeypatch):
    monkeypatch.setattr(settings, "mcp_habilitado", True)
    monkeypatch.setattr(settings, "mcp_url_publica", "https://api.ledgr.teste/mcp")
    monkeypatch.setattr(settings, "oauth_issuer", "https://api.ledgr.teste")
    c = cliente()
    chave = client.post("/chaves-api", headers=c.headers["admin"], json={"nome": "ERP"}).json()
    _inativar(db_session, c.conta_id)

    liberadas = [
        client.patch(
            "/me/preferencias", headers=c.headers["gestor"], json={"roteiro_dispensado": True}
        ),
        client.put(f"/empresas/{c.empresa_id}/favorita", headers=c.headers["gestor"]),
        client.delete(f"/empresas/{c.empresa_id}/favorita", headers=c.headers["gestor"]),
        client.delete(f"/chaves-api/{chave['id']}", headers=c.headers["admin"]),
        client.delete(f"/conexoes-mcp/{uuid.uuid4()}", headers=c.headers["admin"]),
        client.delete(f"/fechamentos/2026-09/links/{uuid.uuid4()}", headers=c.headers["admin"]),
        client.delete(f"/empresa/usuarios/{c.usuarios['analista'].id}", headers=c.headers["admin"]),
        client.post(
            "/me/senha",
            headers=c.headers["gestor"],
            json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": "Outra-senha-forte-123"},
        ),
    ]

    for resposta in liberadas:
        assert resposta.status_code != 403, resposta.request.url
        assert resposta.json() != INATIVA if resposta.content else True
    db_session.expire_all()
    assert db_session.get(Usuario, c.usuarios["analista"].id).removido_em is not None


def test_reativar_devolve_as_escritas(db_session, cliente):
    c = cliente()
    _inativar(db_session, c.conta_id)
    assert (
        client.post("/chaves-api", headers=c.headers["admin"], json={"nome": "A"}).status_code
        == 403
    )

    _reativar(db_session, c.conta_id)

    assert (
        client.post("/chaves-api", headers=c.headers["admin"], json={"nome": "A"}).status_code
        == 201
    )


def test_api_publica_para_na_conta_inativa(db_session, cliente):
    c = cliente()
    chave = client.post("/chaves-api", headers=c.headers["admin"], json={"nome": "ERP"}).json()
    autorizacao = {"Authorization": f"Bearer {chave['chave']}"}
    assert client.get("/v1/empresas", headers=autorizacao).status_code == 200

    _inativar(db_session, c.conta_id)
    parada = client.get("/v1/empresas", headers=autorizacao)
    _reativar(db_session, c.conta_id)
    voltou = client.get("/v1/empresas", headers=autorizacao)

    assert (parada.status_code, parada.json()) == (403, INATIVA)
    assert voltou.status_code == 200


def _convite(db_session, c, email: str, token: str | None = None) -> None:
    db_session.add(
        Convite(
            conta_id=c.conta_id,
            papel="analista",
            email=email,
            token_hash=hashlib.sha256((token or uuid.uuid4().hex).encode()).hexdigest(),
            convidado_por_id=c.usuarios["admin"].id,
            expira_em=func.now() + timedelta(days=7),
        )
    )
    db_session.commit()


def test_convite_nao_e_aceito_na_conta_inativa(db_session, cliente):
    c = cliente()
    token = secrets.token_urlsafe(32)
    _convite(db_session, c, "aceite@cliente.com.br", token)
    _inativar(db_session, c.conta_id)

    consulta = client.post("/convites/consultar", json={"token": token})
    aceite = client.post(
        "/convites/aceitar",
        json={"token": token, "nome": "Novo Usuário", "senha": "Senha-forte-123"},
    )

    assert consulta.status_code == 200
    assert (aceite.status_code, aceite.json()) == (403, INATIVA)
    novo = db_session.scalar(select(Usuario.id).where(Usuario.email == "aceite@cliente.com.br"))
    assert novo is None


def test_link_publico_da_conta_inativa_e_404_igual(db_session, cliente, monkeypatch):
    monkeypatch.setattr(settings, "link_publico_habilitado", True)
    from app.models import Fechamento, LinkRelatorio
    from app.services import relatorio_publico

    c = cliente()
    fechamento = Fechamento(
        empresa_id=c.empresa_id,
        competencia="2026-09",
        resumo={"pares": [], "contagens": {}, "justificadas": 0, "pendentes": 0},
        fechado_por_nome="Ana",
    )
    db_session.add(fechamento)
    db_session.flush()
    novo = relatorio_publico.gerar_token()
    db_session.add(
        LinkRelatorio(
            empresa_id=c.empresa_id,
            fechamento_id=fechamento.id,
            prefixo=novo.prefixo,
            hash=novo.hash,
            criado_por_nome="Ana",
            expira_em=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db_session.commit()
    assert relatorio_publico.link_valido(db_session, novo.token) is not None

    _inativar(db_session, c.conta_id)
    inativa = client.post("/relatorios-publicos/consultar", json={"token": novo.token})
    invalido = client.post("/relatorios-publicos/consultar", json={"token": "x" * 43})

    assert inativa.status_code == 404
    assert inativa.content == invalido.content


def test_geracao_automatica_para_antes_da_proxima_linha(db_session, cliente):
    c = cliente()

    class ProvedorQueNaoPodeSerChamado:
        def explicar_divergencia(self, *args, **kwargs):  # pragma: no cover - não pode rodar
            raise AssertionError("o provedor não pode ser chamado com a conta inativa")

    preparo = geracao.Preparo(
        empresa_id=c.empresa_id,
        conciliacao_id=uuid.uuid4(),
        status="divergente_valor",
        contexto=None,
        chave="c" * 64,
    )
    _inativar(db_session, c.conta_id)

    resultado = geracao._gerar_em_sessao_propria(
        preparo, ProvedorQueNaoPodeSerChamado(), geracao.SessionLocal
    )

    assert resultado is None


def test_ultimo_login_e_gravado(db_session, cliente):
    c = cliente()
    gestor = c.usuarios["gestor"]
    assert db_session.get(Usuario, gestor.id).ultimo_login_em is None

    resposta = client.post("/login", json={"email": gestor.email, "senha": SENHA_USUARIO_TESTE})

    assert resposta.status_code == 200
    db_session.expire_all()
    assert db_session.get(Usuario, gestor.id).ultimo_login_em is not None


def test_empresa_da_outra_conta_nao_aparece_no_uso(db_session, operador, cliente):
    o = operador()
    c, outra = cliente("Conta A"), cliente("Conta B")
    client.post("/chaves-api", headers=outra.headers["admin"], json={"nome": "B"})

    uso = _detalhe(o.headers, c.conta_id).json()["uso"]

    assert (uso["empresas"], uso["chaves_api_ativas"]) == (2, 0)
    assert (
        db_session.scalar(
            select(func.count()).select_from(Empresa).where(Empresa.conta_id == outra.conta_id)
        )
        == 2
    )
    assert (
        db_session.scalar(
            select(func.count()).select_from(EventoConta).where(EventoConta.conta_id == c.conta_id)
        )
        == 0
    )
