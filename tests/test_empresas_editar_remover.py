"""Editar, remover e restaurar empresa cliente (issue #215, ADR-019): `PATCH` e
`DELETE /empresas/{id}`, `GET /empresas/removidas` e `POST /empresas/{id}/restaurar`.

A empresa removida sumir de todas as outras rotas é de
tests/test_empresas_removidas_isolamento.py; apagar de vez, de
tests/test_apagar_empresas_removidas.py. A BrasilAPI nunca é chamada: os 409 do
cadastro saem antes da consulta à Receita.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import ProvedorEmailFalso, _gerar_cnpj, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api.senha import obter_provedor_email_dependencia
from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    ClienteOAuth,
    ConexaoMcp,
    Conta,
    Empresa,
    EmpresaFavorita,
    Extrato,
    TokenOAuth,
)
from app.models.empresa import PRAZO_PARA_APAGAR
from app.services import empresas_removidas
from main import app

client = TestClient(app)

NAO_ENCONTRADA = {"detail": "Empresa não encontrada."}


@pytest.fixture
def conta(db_session, criar_usuario, criar_empresa, gerar_token):
    """Admin, gestor e analista; a empresa principal (a do cadastro, com o CNPJ
    da conta) e duas empresas clientes."""
    admin = criar_usuario(nome="Ana Admin")
    usuarios = {
        "admin": admin,
        "gestor": criar_usuario(conta_id=admin.conta_id, nome="Gil Gestor", papel="gestor"),
        "analista": criar_usuario(conta_id=admin.conta_id, nome="Ada Analista", papel="analista"),
    }
    conta_id = admin.conta_id
    principal = db_session.scalars(select(Empresa).where(Empresa.conta_id == conta_id)).one()
    return SimpleNamespace(
        id=conta_id,
        usuarios=usuarios,
        headers={
            papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id))
            for papel, u in usuarios.items()
        },
        principal=principal,
        cliente_a=criar_empresa("Cliente A Ltda", conta_id=conta_id),
        cliente_b=criar_empresa("Cliente B Ltda", conta_id=conta_id),
    )


def _banco(db_session, empresa_id) -> Empresa:
    """A empresa como está no banco, removida ou não."""
    db_session.expire_all()
    return db_session.scalar(
        select(Empresa).where(Empresa.id == empresa_id).execution_options(incluir_removidas=True)
    )


def _patch(headers, empresa_id, corpo):
    return client.patch(f"/empresas/{empresa_id}", headers=headers, json=corpo)


def _remover(headers, empresa_id):
    return client.delete(f"/empresas/{empresa_id}", headers=headers)


def _restaurar(headers, empresa_id):
    return client.post(f"/empresas/{empresa_id}/restaurar", headers=headers)


def _ids_da_lista(headers):
    return {i["id"] for i in client.get("/empresas?limit=100", headers=headers).json()["itens"]}


def _me(headers) -> dict:
    return client.get("/me", headers=headers).json()


@pytest.fixture
def relogio(monkeypatch):
    """`relogio(dias=31)` adianta o relógio de `empresas_removidas.agora`."""

    def _adiantar(**intervalo):
        monkeypatch.setattr(
            empresas_removidas, "agora", lambda: datetime.now(UTC) + timedelta(**intervalo)
        )

    return _adiantar


# --- PATCH -----------------------------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor"])
def test_admin_e_gestor_editam_e_o_nome_novo_aparece_nas_listas(db_session, conta, papel):
    resposta = _patch(conta.headers[papel], conta.cliente_a.id, {"razao_social": "  Nome Novo  "})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["id"] == str(conta.cliente_a.id)
    assert corpo["razao_social"] == "Nome Novo"
    assert set(corpo) == {"id", "razao_social", "cnpj", "cnpj_verificado_em", "criado_em"}
    na_lista = {
        i["id"]: i["razao_social"]
        for i in client.get("/empresas", headers=conta.headers["analista"]).json()["itens"]
    }
    assert na_lista[str(conta.cliente_a.id)] == "Nome Novo"
    no_me = {e["id"]: e["razao_social"] for e in _me(conta.headers["analista"])["empresas"]}
    assert no_me[str(conta.cliente_a.id)] == "Nome Novo"


def test_o_cnpj_nao_muda(db_session, conta):
    cnpj = conta.cliente_a.cnpj

    resposta = _patch(conta.headers["admin"], conta.cliente_a.id, {"razao_social": "Outro"})

    assert resposta.json()["cnpj"] == cnpj
    assert _banco(db_session, conta.cliente_a.id).cnpj == cnpj


def test_analista_recebe_403(db_session, conta):
    resposta = _patch(conta.headers["analista"], conta.cliente_a.id, {"razao_social": "X"})

    assert resposta.status_code == 403
    assert _banco(db_session, conta.cliente_a.id).razao_social == "Cliente A Ltda"


def test_empresa_de_outra_conta_inexistente_ou_removida_e_404(db_session, conta, criar_empresa):
    outra = criar_empresa("De outra conta")
    assert _remover(conta.headers["admin"], conta.cliente_b.id).status_code == 200

    for empresa_id in (outra.id, uuid.uuid4(), conta.cliente_b.id):
        resposta = _patch(conta.headers["admin"], empresa_id, {"razao_social": "X"})
        assert resposta.status_code == 404, empresa_id
        assert resposta.json() == NAO_ENCONTRADA
    assert _banco(db_session, outra.id).razao_social == "De outra conta"
    assert _banco(db_session, conta.cliente_b.id).razao_social == "Cliente B Ltda"


@pytest.mark.parametrize("corpo", [{"razao_social": ""}, {"razao_social": "   "}])
def test_razao_social_vazia_ou_so_espacos_e_422(conta, corpo):
    resposta = _patch(conta.headers["admin"], conta.cliente_a.id, corpo)

    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["loc"] == ["body", "razao_social"]


@pytest.mark.parametrize(
    "corpo",
    [
        {"cnpj": "11222333000181"},
        {"razao_social": "Ok", "nome_exibicao": "Filial"},
        {"razao_social": "Ok", "id": str(uuid.uuid4())},
        {},
        {"razao_social": None},
    ],
)
def test_campo_desconhecido_corpo_vazio_e_null_sao_422(db_session, conta, corpo):
    cnpj = conta.cliente_a.cnpj

    resposta = _patch(conta.headers["admin"], conta.cliente_a.id, corpo)

    assert resposta.status_code == 422, resposta.text
    banco = _banco(db_session, conta.cliente_a.id)
    assert (banco.razao_social, banco.cnpj) == ("Cliente A Ltda", cnpj)


def test_editar_a_principal_muda_a_razao_social_da_conta_junto(db_session, conta):
    resposta = _patch(conta.headers["gestor"], conta.principal.id, {"razao_social": "Matriz SA"})

    assert resposta.status_code == 200, resposta.text
    db_session.expire_all()
    assert db_session.get(Conta, conta.id).razao_social == "Matriz SA"
    assert _me(conta.headers["admin"])["razao_social"] == "Matriz SA"
    assert _banco(db_session, conta.principal.id).razao_social == "Matriz SA"


def test_editar_outra_empresa_nao_mexe_na_conta(db_session, conta):
    antes = db_session.get(Conta, conta.id).razao_social

    _patch(conta.headers["admin"], conta.cliente_a.id, {"razao_social": "Filial Nova"})

    db_session.expire_all()
    assert db_session.get(Conta, conta.id).razao_social == antes
    assert _banco(db_session, conta.principal.id).razao_social == antes


def test_conta_inativa_nao_edita(db_session, conta):
    db_session.execute(
        update(Conta)
        .where(Conta.id == conta.id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()

    resposta = _patch(conta.headers["admin"], conta.cliente_a.id, {"razao_social": "X"})

    assert resposta.status_code == 403
    assert resposta.json()["codigo"] == "conta_inativa"
    assert _banco(db_session, conta.cliente_a.id).razao_social == "Cliente A Ltda"


def test_patch_sem_token_e_401(conta):
    assert (
        client.patch(f"/empresas/{conta.cliente_a.id}", json={"razao_social": "X"}).status_code
        == 401
    )


# --- DELETE ----------------------------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor"])
def test_remover_desativa_na_hora_e_guarda_quem_quando_e_o_prazo(db_session, conta, papel):
    antes = datetime.now(UTC)

    resposta = _remover(conta.headers[papel], conta.cliente_a.id)

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    quem = conta.usuarios[papel]
    assert corpo["id"] == str(conta.cliente_a.id)
    assert corpo["razao_social"] == "Cliente A Ltda"
    assert corpo["cnpj"] == conta.cliente_a.cnpj
    assert corpo["removida_por_nome"] == quem.nome
    removida_em = datetime.fromisoformat(corpo["removida_em"])
    assert antes <= removida_em <= datetime.now(UTC)
    assert datetime.fromisoformat(corpo["apagar_em"]) - removida_em == PRAZO_PARA_APAGAR
    banco = _banco(db_session, conta.cliente_a.id)
    assert banco.removida_por_id == quem.id
    assert banco.removida_por_nome == quem.nome
    assert banco.removida_em is not None
    assert banco.apagar_em - banco.removida_em == PRAZO_PARA_APAGAR


def test_analista_nao_remove(db_session, conta):
    resposta = _remover(conta.headers["analista"], conta.cliente_a.id)

    assert resposta.status_code == 403
    assert _banco(db_session, conta.cliente_a.id).removida_em is None


def test_empresa_de_outra_conta_inexistente_ou_ja_removida_e_404(conta, criar_empresa):
    outra = criar_empresa("De outra conta")
    assert _remover(conta.headers["admin"], conta.cliente_a.id).status_code == 200

    for empresa_id in (outra.id, uuid.uuid4(), conta.cliente_a.id):
        resposta = _remover(conta.headers["admin"], empresa_id)
        assert resposta.status_code == 404, empresa_id
        assert resposta.json() == NAO_ENCONTRADA


def test_a_principal_nao_pode_ser_removida(db_session, conta):
    resposta = _remover(conta.headers["admin"], conta.principal.id)

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": empresas_removidas.MENSAGEM_PRINCIPAL,
        "codigo": "empresa_principal",
    }
    assert _banco(db_session, conta.principal.id).removida_em is None


def test_a_ultima_empresa_ativa_nao_pode_ser_removida(
    db_session, criar_conta, criar_empresa, criar_usuario, gerar_token
):
    """Conta cuja única empresa não tem o CNPJ da conta: não é a principal, mas é a última."""
    conta_sem_principal = criar_conta("Conta", _gerar_cnpj())
    unica = criar_empresa("Única", conta_id=conta_sem_principal.id)
    admin = criar_usuario(conta_id=conta_sem_principal.id, papel="admin")
    headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id))

    resposta = _remover(headers, unica.id)

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": empresas_removidas.MENSAGEM_ULTIMA,
        "codigo": "ultima_empresa",
    }
    assert _banco(db_session, unica.id).removida_em is None


def test_a_ultima_conta_depois_de_remover_as_outras(db_session, conta):
    """Principal + duas clientes: as clientes saem; a que sobra é a principal."""
    assert _remover(conta.headers["admin"], conta.cliente_a.id).status_code == 200
    assert _remover(conta.headers["admin"], conta.cliente_b.id).status_code == 200

    resposta = _remover(conta.headers["admin"], conta.principal.id)

    assert resposta.status_code == 409
    assert resposta.json()["codigo"] == "empresa_principal"
    assert _ids_da_lista(conta.headers["admin"]) == {str(conta.principal.id)}


def test_a_remocao_tira_a_empresa_das_listas_e_do_limite(db_session, conta):
    assert _me(conta.headers["admin"])["limites"]["empresas"]["usadas"] == 3

    _remover(conta.headers["gestor"], conta.cliente_a.id)

    assert str(conta.cliente_a.id) not in _ids_da_lista(conta.headers["analista"])
    me = _me(conta.headers["analista"])
    assert str(conta.cliente_a.id) not in {e["id"] for e in me["empresas"]}
    assert me["limites"]["empresas"]["usadas"] == 2
    # A busca por nome e por CNPJ também não acha.
    for busca in ("Cliente A", conta.cliente_a.cnpj):
        itens = client.get("/empresas", params={"busca": busca}, headers=conta.headers["admin"])
        assert itens.json()["total"] == 0


def test_favorita_removida_some_e_nao_se_favorita_de_novo(db_session, conta):
    client.put(f"/empresas/{conta.cliente_a.id}/favorita", headers=conta.headers["admin"])
    _remover(conta.headers["admin"], conta.cliente_a.id)

    lista = client.get("/empresas", headers=conta.headers["admin"]).json()["itens"]
    assert all(i["favorita"] is False for i in lista)
    for metodo in (client.put, client.delete):
        resposta = metodo(
            f"/empresas/{conta.cliente_a.id}/favorita", headers=conta.headers["admin"]
        )
        assert resposta.status_code == 404


def test_o_cabecalho_da_empresa_removida_da_404_igual_ao_de_outra_conta(conta, criar_empresa):
    outra = criar_empresa("De outra conta")
    _remover(conta.headers["admin"], conta.cliente_a.id)

    for empresa_id in (conta.cliente_a.id, outra.id):
        resposta = client.get(
            "/extratos", headers={**conta.headers["admin"], CABECALHO_EMPRESA: str(empresa_id)}
        )
        assert resposta.status_code == 404
        assert resposta.json() == NAO_ENCONTRADA
    ativa = client.get(
        "/extratos",
        headers={**conta.headers["admin"], CABECALHO_EMPRESA: str(conta.cliente_b.id)},
    )
    assert ativa.status_code == 200


def test_sem_o_cabecalho_a_conta_que_ficou_com_uma_empresa_so_dispensa_o_cabecalho(conta):
    assert client.get("/extratos", headers=conta.headers["admin"]).status_code == 400
    _remover(conta.headers["admin"], conta.cliente_a.id)
    _remover(conta.headers["admin"], conta.cliente_b.id)

    assert client.get("/extratos", headers=conta.headers["admin"]).status_code == 200


def test_o_extrato_da_empresa_removida_nao_e_apagado_na_hora(
    db_session, conta, criar_extrato_da_empresa
):
    extrato = criar_extrato_da_empresa(conta.cliente_a.id, "banco")

    _remover(conta.headers["admin"], conta.cliente_a.id)

    db_session.expire_all()
    assert db_session.get(Extrato, extrato.id) is not None


def test_conta_inativa_nao_remove_nem_restaura(db_session, conta):
    _remover(conta.headers["admin"], conta.cliente_b.id)
    db_session.execute(
        update(Conta)
        .where(Conta.id == conta.id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()

    for resposta in (
        _remover(conta.headers["admin"], conta.cliente_a.id),
        _restaurar(conta.headers["admin"], conta.cliente_b.id),
    ):
        assert resposta.status_code == 403
        assert resposta.json()["codigo"] == "conta_inativa"
    assert _banco(db_session, conta.cliente_a.id).removida_em is None
    assert _banco(db_session, conta.cliente_b.id).removida_em is not None


def test_remover_sem_token_e_401(conta):
    assert client.delete(f"/empresas/{conta.cliente_a.id}").status_code == 401


# --- conexões MCP ----------------------------------------------------------------------


def _conexao(db_session, conta, empresa) -> SimpleNamespace:
    """Uma conexão MCP ativa da empresa, com um token de acesso válido."""
    cliente_id = f"cliente-{uuid.uuid4().hex[:8]}"
    db_session.add(
        ClienteOAuth(
            client_id=cliente_id, client_name="Claude", redirect_uris=["https://x.test/cb"]
        )
    )
    db_session.flush()
    conexao = ConexaoMcp(
        conta_id=conta.id,
        empresa_id=empresa.id,
        client_id=cliente_id,
        nome_cliente="Claude",
        escopo="ledgr.leitura",
        criada_por_id=conta.usuarios["admin"].id,
    )
    db_session.add(conexao)
    db_session.flush()
    token = TokenOAuth(
        conexao_id=conexao.id,
        client_id=cliente_id,
        hash_acesso=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        scope="ledgr.leitura",
        resource="https://api.ledgr.teste/mcp",
        acesso_expira_em=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(token)
    db_session.commit()
    return SimpleNamespace(id=conexao.id, token_id=token.id)


def _revogada(db_session, conexao) -> tuple[bool, bool]:
    db_session.expire_all()
    c = db_session.get(ConexaoMcp, conexao.id)
    t = db_session.get(TokenOAuth, conexao.token_id)
    return c.revogada_em is not None, t.revogado_em is not None


def test_as_conexoes_mcp_da_empresa_sao_revogadas_na_hora_e_as_outras_ficam(db_session, conta):
    da_empresa = _conexao(db_session, conta, conta.cliente_a)
    da_outra = _conexao(db_session, conta, conta.cliente_b)

    _remover(conta.headers["admin"], conta.cliente_a.id)

    assert _revogada(db_session, da_empresa) == (True, True)
    assert _revogada(db_session, da_outra) == (False, False)
    db_session.expire_all()
    assert db_session.get(ConexaoMcp, da_empresa.id).revogada_por_id == conta.usuarios["admin"].id


# --- GET /empresas/removidas -----------------------------------------------------------


def test_lista_as_removidas_com_quem_quando_e_o_prazo_da_mais_nova_para_a_mais_antiga(conta):
    _remover(conta.headers["admin"], conta.cliente_a.id)
    _remover(conta.headers["gestor"], conta.cliente_b.id)

    for papel in ("admin", "gestor"):
        resposta = client.get("/empresas/removidas", headers=conta.headers[papel])
        assert resposta.status_code == 200
        itens = resposta.json()["itens"]
        assert [i["id"] for i in itens] == [str(conta.cliente_b.id), str(conta.cliente_a.id)]
        assert set(itens[0]) == {
            "id",
            "razao_social",
            "cnpj",
            "removida_em",
            "removida_por_nome",
            "apagar_em",
        }
        assert itens[0]["removida_por_nome"] == "Gil Gestor"
        assert itens[1]["removida_por_nome"] == "Ana Admin"


def test_a_lista_de_removidas_nao_mostra_ativa_nem_de_outra_conta_nem_vencida(
    db_session, conta, criar_empresa, criar_usuario, gerar_token, relogio
):
    outra_admin = criar_usuario(nome="Outra Admin")
    outra_cliente = criar_empresa("Outra cliente", conta_id=outra_admin.conta_id)
    outros = cabecalhos(gerar_token(outra_admin.conta_id, usuario_id=outra_admin.id))
    _remover(outros, outra_cliente.id)
    _remover(conta.headers["admin"], conta.cliente_a.id)

    itens = client.get("/empresas/removidas", headers=conta.headers["admin"]).json()["itens"]
    assert [i["id"] for i in itens] == [str(conta.cliente_a.id)]

    relogio(days=31)
    vazia = client.get("/empresas/removidas", headers=conta.headers["admin"]).json()
    assert vazia == {"itens": []}


def test_analista_nao_lista_as_removidas(conta):
    assert client.get("/empresas/removidas", headers=conta.headers["analista"]).status_code == 403


# --- POST /empresas/{id}/restaurar -----------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor"])
def test_restaurar_volta_tudo_como_estava(db_session, conta, criar_extrato_da_empresa, papel):
    extrato = criar_extrato_da_empresa(conta.cliente_a.id, "banco")
    client.put(f"/empresas/{conta.cliente_a.id}/favorita", headers=conta.headers["admin"])
    antes = _banco(db_session, conta.cliente_a.id)
    razao, cnpj, criado_em = antes.razao_social, antes.cnpj, antes.criado_em
    _remover(conta.headers["admin"], conta.cliente_a.id)

    resposta = _restaurar(conta.headers[papel], conta.cliente_a.id)

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["razao_social"] == razao
    banco = _banco(db_session, conta.cliente_a.id)
    assert (banco.removida_em, banco.removida_por_id, banco.removida_por_nome, banco.apagar_em) == (
        None,
        None,
        None,
        None,
    )
    assert (banco.razao_social, banco.cnpj, banco.criado_em) == (razao, cnpj, criado_em)
    assert db_session.get(Extrato, extrato.id) is not None
    lista = {
        i["id"]: i for i in client.get("/empresas", headers=conta.headers["admin"]).json()["itens"]
    }
    assert lista[str(conta.cliente_a.id)]["favorita"] is True
    assert _me(conta.headers["admin"])["limites"]["empresas"]["usadas"] == 3
    assert client.get("/empresas/removidas", headers=conta.headers["admin"]).json() == {"itens": []}
    voltou = client.get(
        "/extratos", headers={**conta.headers["admin"], CABECALHO_EMPRESA: str(conta.cliente_a.id)}
    )
    assert voltou.status_code == 200
    assert db_session.scalar(
        select(EmpresaFavorita.empresa_id).where(EmpresaFavorita.empresa_id == conta.cliente_a.id)
    )


def test_restaurar_nao_devolve_as_conexoes_mcp(db_session, conta):
    conexao = _conexao(db_session, conta, conta.cliente_a)
    _remover(conta.headers["admin"], conta.cliente_a.id)

    _restaurar(conta.headers["admin"], conta.cliente_a.id)

    assert _revogada(db_session, conexao) == (True, True)


def test_restaurar_respeita_o_limite_de_empresas_do_plano(db_session, conta, monkeypatch):
    _remover(conta.headers["admin"], conta.cliente_a.id)
    # Com a vaga ocupada por outra empresa no meio-tempo: 2 ativas, limite de 2.
    monkeypatch.setattr(settings, "limite_empresas_essencial", 2)

    resposta = _restaurar(conta.headers["admin"], conta.cliente_a.id)

    assert resposta.status_code == 402
    assert resposta.json() == {"detail": "Sua conta chegou ao limite de empresas do plano."}
    assert _banco(db_session, conta.cliente_a.id).removida_em is not None

    monkeypatch.setattr(settings, "limite_empresas_essencial", 3)
    assert _restaurar(conta.headers["admin"], conta.cliente_a.id).status_code == 200


def test_depois_do_prazo_nao_restaura(db_session, conta, relogio):
    _remover(conta.headers["admin"], conta.cliente_a.id)
    relogio(days=30, seconds=1)

    resposta = _restaurar(conta.headers["admin"], conta.cliente_a.id)

    assert resposta.status_code == 404
    assert resposta.json() == NAO_ENCONTRADA
    assert _banco(db_session, conta.cliente_a.id).removida_em is not None


def test_no_ultimo_dia_ainda_restaura(conta, relogio):
    _remover(conta.headers["admin"], conta.cliente_a.id)
    relogio(days=29, hours=23)

    assert _restaurar(conta.headers["admin"], conta.cliente_a.id).status_code == 200


def test_restaurar_analista_ativa_outra_conta_e_inexistente(conta, criar_empresa):
    outra = criar_empresa("De outra conta")
    _remover(conta.headers["admin"], conta.cliente_a.id)

    assert _restaurar(conta.headers["analista"], conta.cliente_a.id).status_code == 403
    for empresa_id in (conta.cliente_b.id, outra.id, uuid.uuid4()):
        resposta = _restaurar(conta.headers["admin"], empresa_id)
        assert resposta.status_code == 404, empresa_id


def test_restaurar_empresa_removida_de_outra_conta_e_404(
    conta, criar_empresa, criar_usuario, gerar_token
):
    outra_admin = criar_usuario(nome="Outra Admin")
    outra_cliente = criar_empresa("Outra cliente", conta_id=outra_admin.conta_id)
    outros = cabecalhos(gerar_token(outra_admin.conta_id, usuario_id=outra_admin.id))
    _remover(outros, outra_cliente.id)

    assert _restaurar(conta.headers["admin"], outra_cliente.id).status_code == 404


# --- cadastrar de novo o CNPJ de uma empresa removida ----------------------------------


def _cadastrar(headers, cnpj):
    return client.post(
        "/empresas", headers=headers, json={"cnpj": cnpj, "razao_social": "De novo Ltda"}
    )


def test_cadastrar_o_cnpj_de_empresa_removida_da_409_com_o_id_para_restaurar(conta):
    _remover(conta.headers["admin"], conta.cliente_a.id)

    resposta = _cadastrar(conta.headers["admin"], conta.cliente_a.cnpj)

    assert resposta.status_code == 409
    corpo = resposta.json()
    assert corpo["codigo"] == "empresa_removida"
    assert corpo["empresa_id"] == str(conta.cliente_a.id)
    assert "restaurada" in corpo["detail"]
    assert datetime.fromisoformat(corpo["apagar_em"]) > datetime.now(UTC)
    # O front oferece restaurar, e restaurar funciona.
    assert _restaurar(conta.headers["admin"], corpo["empresa_id"]).status_code == 200


def test_cadastrar_o_cnpj_de_empresa_ativa_continua_409_sem_codigo(conta):
    resposta = _cadastrar(conta.headers["admin"], conta.cliente_a.cnpj)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": "Empresa já cadastrada nesta conta."}


def test_cadastrar_o_cnpj_de_removida_com_prazo_vencido_diz_que_esta_sendo_apagada(conta, relogio):
    _remover(conta.headers["admin"], conta.cliente_a.id)
    relogio(days=31)

    resposta = _cadastrar(conta.headers["admin"], conta.cliente_a.cnpj)

    assert resposta.status_code == 409
    assert resposta.json()["codigo"] == "empresa_sendo_apagada"
    assert "empresa_id" not in resposta.json()


def test_remover_nao_devolve_a_cota_do_cadastro_por_hora(db_session, conta, monkeypatch):
    from app.api import empresas

    # A conta já criou 3 empresas na última hora (principal e duas clientes).
    monkeypatch.setattr(empresas, "LIMITE_EMPRESAS_POR_HORA", 3)
    _remover(conta.headers["admin"], conta.cliente_a.id)

    resposta = _cadastrar(conta.headers["admin"], _gerar_cnpj())

    assert resposta.status_code == 429


# --- e-mail ----------------------------------------------------------------------------


def test_o_gestor_que_remove_avisa_o_admin_e_a_si_depois_do_commit(db_session, conta, provedor):
    vistos_no_envio = []
    enviar_original = provedor.enviar

    def enviar(mensagem):
        # O envio roda depois do commit: outra sessão já enxerga a remoção.
        with SessionLocal() as outra:
            vistos_no_envio.append(
                outra.scalar(
                    select(Empresa.removida_em)
                    .where(Empresa.id == conta.cliente_a.id)
                    .execution_options(incluir_removidas=True)
                )
            )
        enviar_original(mensagem)

    provedor.enviar = enviar

    resposta = _remover(conta.headers["gestor"], conta.cliente_a.id)

    assert resposta.status_code == 200
    assert {m.para for m in provedor.enviadas} == {
        conta.usuarios["admin"].email,
        conta.usuarios["gestor"].email,
    }
    assert all(vistos_no_envio) and len(vistos_no_envio) == 2
    apagar_em = datetime.fromisoformat(resposta.json()["apagar_em"]).astimezone(
        empresas_removidas.FUSO
    )
    for mensagem in provedor.enviadas:
        assert "Cliente A Ltda" in mensagem.texto and "Gil Gestor" in mensagem.texto
        assert apagar_em.strftime("%d/%m/%Y") in mensagem.texto
        assert "Cliente A Ltda" in mensagem.html
        assert conta.cliente_a.cnpj not in mensagem.texto + mensagem.html
    assert len({m.chave_idempotencia for m in provedor.enviadas}) == 2


def test_o_admin_que_remove_recebe_um_aviso_so(conta, provedor):
    _remover(conta.headers["admin"], conta.cliente_a.id)

    assert [m.para for m in provedor.enviadas] == [conta.usuarios["admin"].email]


def test_falha_no_envio_nao_desfaz_a_remocao(db_session, conta):
    falho = ProvedorEmailFalso(falha=RuntimeError("provedor fora do ar"))
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: falho
    try:
        resposta = _remover(conta.headers["admin"], conta.cliente_a.id)
    finally:
        app.dependency_overrides.pop(obter_provedor_email_dependencia, None)

    assert resposta.status_code == 200
    assert _banco(db_session, conta.cliente_a.id).removida_em is not None
    assert str(conta.cliente_a.id) not in _ids_da_lista(conta.headers["admin"])


def test_sem_provedor_de_email_a_remocao_vale_sem_aviso(db_session, conta):
    resposta = _remover(conta.headers["admin"], conta.cliente_a.id)

    assert resposta.status_code == 200
    assert _banco(db_session, conta.cliente_a.id).removida_em is not None


def test_recusa_nao_manda_aviso(conta, provedor):
    _remover(conta.headers["admin"], conta.principal.id)
    _remover(conta.headers["analista"], conta.cliente_a.id)

    assert provedor.enviadas == []


def test_o_log_nao_tem_cnpj_nem_razao_social(conta, caplog):
    caplog.set_level("INFO")

    _remover(conta.headers["admin"], conta.cliente_a.id)
    _restaurar(conta.headers["admin"], conta.cliente_a.id)
    _patch(conta.headers["admin"], conta.cliente_a.id, {"razao_social": "Nome Secreto"})

    texto = caplog.text
    assert "remover_empresa resultado=removida" in texto
    assert "restaurar_empresa resultado=restaurada" in texto
    for proibido in (conta.cliente_a.cnpj, "Cliente A", "Nome Secreto", "Ana Admin"):
        assert proibido not in texto


# --- limite de taxa --------------------------------------------------------------------


def test_editar_remover_e_restaurar_tem_limite_por_usuario_para_nao_encher_a_caixa_do_admin(
    conta, provedor
):
    from app.api import empresas
    from app.core.rate_limit import limiter

    limite = int(empresas.LIMITE_EMPRESA_ESCRITA_POR_USUARIO.split("/")[0])
    for _ in range(limite):
        assert _remover(conta.headers["admin"], conta.cliente_a.id).status_code == 200
        assert _restaurar(conta.headers["admin"], conta.cliente_a.id).status_code == 200

    # A 21ª remoção e a 21ª restauração são barradas, e nenhum e-mail a mais sai.
    assert _remover(conta.headers["admin"], conta.cliente_a.id).status_code == 429
    assert _restaurar(conta.headers["admin"], conta.cliente_a.id).status_code == 429
    assert len(provedor.enviadas) == limite
    limiter.reset()
