"""Testes de `GET` e `DELETE /empresa/usuarios` (issue #69, ADR-015) e do
usuário removido: sem login, 401 nas rotas que carregam o usuário, histórico
preservado e e-mail liberado. Desde a issue #133, usuários são da conta, cada
um com o `papel`, e só o admin remove.
"""

import hashlib
import uuid
from datetime import timedelta

import pytest
from conftest import SENHA_USUARIO_TESTE, apagar_contas_por_cnpj, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import DecisaoLinha, Fechamento, TokenEmail, Usuario
from app.services.usuarios import email_de_removido
from main import app

client = TestClient(app)

USUARIO_NAO_ENCONTRADO = {"detail": "Usuário não encontrado."}


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


@pytest.fixture
def equipe(db_session, criar_usuario):
    """Admin e um convidado (gestor) na mesma conta."""
    admin = criar_usuario(nome="Ana Administradora")
    convidado = criar_usuario(conta_id=admin.conta_id, nome="Bruno Convidado")
    return admin, convidado


def _remover(cabecalho, usuario_id):
    return client.delete(f"/empresa/usuarios/{usuario_id}", headers=cabecalho)


def _recarregar(db_session, usuario: Usuario) -> Usuario:
    db_session.expire_all()
    return db_session.get(Usuario, usuario.id)


# --- listagem ------------------------------------------------------------------


def test_qualquer_usuario_lista_os_ativos_da_propria_conta(
    db_session, equipe, criar_usuario, cabecalho_de
):
    admin, convidado = equipe
    analista = criar_usuario(conta_id=admin.conta_id, nome="Carla Analista", papel="analista")
    criar_usuario()  # outra conta

    for quem in (admin, convidado, analista):
        response = client.get("/empresa/usuarios", headers=cabecalho_de(quem))

        assert response.status_code == 200
        itens = response.json()["itens"]
        assert [
            (u["id"], u["nome"], u["email"], u["papel"], u["administrador"]) for u in itens
        ] == [
            (str(admin.id), admin.nome, admin.email, "admin", True),
            (str(convidado.id), convidado.nome, convidado.email, "gestor", False),
            (str(analista.id), analista.nome, analista.email, "analista", False),
        ]
        assert set(itens[0]) == {"id", "nome", "email", "papel", "administrador", "criado_em"}
        assert itens[0]["criado_em"].endswith(("Z", "+00:00"))


def test_listagem_sem_token_e_401():
    assert client.get("/empresa/usuarios").status_code == 401


# --- remoção -------------------------------------------------------------------


def test_remover_marca_o_usuario_e_libera_o_email(db_session, equipe, cabecalho_de):
    admin, convidado = equipe
    nome, email = convidado.nome, convidado.email

    response = _remover(cabecalho_de(admin), convidado.id)

    assert response.status_code == 204
    removido = _recarregar(db_session, convidado)
    assert removido.removido_em is not None
    assert removido.nome == nome
    assert removido.email == email_de_removido(removido)
    assert removido.email == f"removido+{removido.id}@ledgr.invalid"
    assert removido.senha_hash is None
    assert removido.google_sub is None
    lista = client.get("/empresa/usuarios", headers=cabecalho_de(admin)).json()["itens"]
    assert [u["id"] for u in lista] == [str(admin.id)]
    assert db_session.scalar(select(Usuario.id).where(Usuario.email == email)) is None


def test_remover_conta_do_google(db_session, criar_usuario, cabecalho_de):
    admin = criar_usuario()
    do_google = criar_usuario(com_senha=False, conta_id=admin.conta_id)

    assert _remover(cabecalho_de(admin), do_google.id).status_code == 204
    assert _recarregar(db_session, do_google).google_sub is None


def test_remocao_mantem_decisoes_e_fechamentos_do_usuario(
    db_session, equipe, cabecalho_de, criar_extrato_da_empresa
):
    admin, convidado = equipe
    empresa_id = empresa_unica(db_session, admin.conta_id)
    banco = criar_extrato_da_empresa(empresa_id, "banco")
    sistema = criar_extrato_da_empresa(empresa_id, "sistema")
    decisao = DecisaoLinha(
        empresa_id=empresa_id,
        extrato_banco_id=banco.id,
        chave="c" * 64,
        tipo="conferida",
        usuario_id=convidado.id,
        autor_nome=convidado.nome,
        rodada=1,
        extrato_sistema_id=sistema.id,
    )
    fechamento = Fechamento(
        empresa_id=empresa_id,
        competencia="2026-09",
        resumo={},
        fechado_por_id=convidado.id,
        fechado_por_nome=convidado.nome,
    )
    db_session.add_all([decisao, fechamento])
    db_session.commit()

    assert _remover(cabecalho_de(admin), convidado.id).status_code == 204

    db_session.expire_all()
    decisao = db_session.get(DecisaoLinha, decisao.id)
    fechamento = db_session.get(Fechamento, fechamento.id)
    assert decisao.usuario_id == convidado.id
    assert decisao.autor_nome == "Bruno Convidado"
    assert fechamento.fechado_por_id == convidado.id
    assert fechamento.fechado_por_nome == "Bruno Convidado"


def test_remocao_invalida_os_links_pendentes_de_tokens_email(db_session, equipe, cabecalho_de):
    admin, convidado = equipe
    token = TokenEmail(
        usuario_id=convidado.id,
        finalidade="recuperacao_senha",
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        expira_em=func.now() + timedelta(minutes=30),
    )
    db_session.add(token)
    db_session.commit()

    _remover(cabecalho_de(admin), convidado.id)

    db_session.expire_all()
    assert db_session.get(TokenEmail, token.id).usado_em is not None


def test_administrador_nao_pode_ser_removido(db_session, equipe, cabecalho_de):
    admin, _ = equipe

    response = _remover(cabecalho_de(admin), admin.id)

    assert response.status_code == 409
    assert response.json() == {"detail": "O admin da conta não pode ser removido."}
    assert _recarregar(db_session, admin).removido_em is None


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_quem_nao_e_admin_recebe_403_ao_remover(
    db_session, equipe, criar_usuario, cabecalho_de, papel
):
    admin, convidado = equipe
    quem = criar_usuario(conta_id=admin.conta_id, papel=papel)
    outro = criar_usuario(conta_id=admin.conta_id, papel="analista")

    for alvo in (outro, admin, convidado):
        response = _remover(cabecalho_de(quem), alvo.id)
        assert response.status_code == 403
        assert response.json() == {"detail": "Só o admin da conta pode fazer isso."}

    assert _recarregar(db_session, outro).removido_em is None


def test_remover_usuario_de_outra_conta_e_404_como_inexistente(
    db_session, equipe, criar_usuario, cabecalho_de
):
    admin, _ = equipe
    admin_b = criar_usuario()
    convidado_b = criar_usuario(conta_id=admin_b.conta_id)

    de_outra = _remover(cabecalho_de(admin), convidado_b.id)
    inexistente = _remover(cabecalho_de(admin), uuid.uuid4())

    assert de_outra.status_code == inexistente.status_code == 404
    assert de_outra.json() == inexistente.json() == USUARIO_NAO_ENCONTRADO
    assert _recarregar(db_session, convidado_b).removido_em is None


def test_remover_duas_vezes_e_404(db_session, equipe, cabecalho_de):
    admin, convidado = equipe
    assert _remover(cabecalho_de(admin), convidado.id).status_code == 204

    response = _remover(cabecalho_de(admin), convidado.id)

    assert response.status_code == 404
    assert response.json() == USUARIO_NAO_ENCONTRADO


# --- usuário removido ------------------------------------------------------------


def test_removido_nao_entra_e_recebe_a_resposta_de_senha_errada(db_session, equipe, cabecalho_de):
    admin, convidado = equipe
    email = convidado.email
    senha_errada = client.post("/login", json={"email": admin.email, "senha": "senha-errada-1"})

    _remover(cabecalho_de(admin), convidado.id)
    response = client.post("/login", json={"email": email, "senha": SENHA_USUARIO_TESTE})

    assert response.status_code == 401
    assert response.json() == senha_errada.json() == {"detail": "E-mail ou senha inválidos."}


def test_removido_recebe_401_nas_rotas_que_carregam_o_usuario(db_session, equipe, cabecalho_de):
    admin, convidado = equipe
    # O JWT já emitido continua assinado. As rotas por empresa também recusam
    # o removido desde a #75 (tests/test_revogacao_sessao.py).
    cabecalho = cabecalho_de(convidado)
    assert client.get("/me", headers=cabecalho).status_code == 200

    _remover(cabecalho_de(admin), convidado.id)

    assert client.get("/me", headers=cabecalho).status_code == 401
    trocar = client.post(
        "/me/senha",
        json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": "senha-nova-456"},
        headers=cabecalho,
    )
    assert trocar.status_code == 401
    assert client.get("/empresa/usuarios", headers=cabecalho).status_code == 401


@pytest.fixture
def limpar_cadastro(db_session):
    """Apaga a conta, a empresa e o usuário que um `/register` do teste criou."""
    cnpjs: list[str] = []
    yield cnpjs.append
    db_session.rollback()
    apagar_contas_por_cnpj(db_session, cnpjs)
    db_session.commit()


def test_email_do_removido_pode_criar_outra_conta(
    db_session, equipe, cabecalho_de, gerar_cnpj, limpar_cadastro
):
    admin, convidado = equipe
    email = convidado.email
    _remover(cabecalho_de(admin), convidado.id)
    cnpj = gerar_cnpj()
    limpar_cadastro(cnpj)

    response = client.post(
        "/register",
        json={
            "nome": "Bruno de Novo",
            "email": email,
            "senha": "senha-forte-123",
            "razao_social": "Empresa do Bruno",
            "cnpj": cnpj,
        },
    )

    assert response.status_code == 201
    assert response.json()["id"] != str(convidado.id)
    assert (
        client.post("/login", json={"email": email, "senha": "senha-forte-123"}).status_code == 200
    )


def test_email_do_removido_pode_ser_convidado_de_novo_como_usuario_novo(
    db_session, equipe, cabecalho_de, provedor
):
    admin, convidado = equipe
    email = convidado.email
    _remover(cabecalho_de(admin), convidado.id)

    convite = client.post("/empresa/convites", json={"email": email}, headers=cabecalho_de(admin))
    assert convite.status_code == 201
    token = provedor.enviadas[-1].texto.split("#token=", 1)[1].split()[0]
    aceite = client.post(
        "/convites/aceitar", json={"token": token, "nome": "Bruno", "senha": "senha-forte-123"}
    )

    assert aceite.status_code == 201
    assert aceite.json()["id"] != str(convidado.id)
    assert _recarregar(db_session, convidado).removido_em is not None
