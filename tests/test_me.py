"""Testes de GET /me (issue #81): dados da conta pro cabeçalho do front —
nome da empresa, CNPJ e métodos de login. Conta única no MVP, sem campo de
papel (ver app/api/me.py).

Mesmo padrão de tests/test_troca_senha.py: rota por usuário, autenticação e
rate limit testados com as fixtures de conftest.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.me import LIMITE_CONSULTA_ME_POR_USUARIO
from main import app

client = TestClient(app)

LIMITE = int(LIMITE_CONSULTA_ME_POR_USUARIO.split("/")[0])


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario, empresa_id=None) -> dict[str, str]:
        token = gerar_token(empresa_id or usuario.empresa_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _me(cabecalho):
    return client.get("/me", headers=cabecalho)


# --- sucesso ---------------------------------------------------------------


def test_200_com_todos_os_campos_para_usuario_com_senha(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    empresa = usuario.empresa

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    corpo = response.json()
    assert corpo == {
        "id": str(usuario.id),
        "empresa_id": str(usuario.empresa_id),
        "nome": usuario.nome,
        "email": usuario.email,
        "razao_social": empresa.razao_social,
        "cnpj": empresa.cnpj,
        "metodos_login": ["senha"],
    }
    assert len(corpo["cnpj"]) == 14
    assert not corpo["cnpj"].isalpha()  # sem máscara (sem pontuação)
    assert "." not in corpo["cnpj"] and "/" not in corpo["cnpj"] and "-" not in corpo["cnpj"]


def test_conta_sem_senha_devolve_metodo_google(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["metodos_login"] == ["google"]


def test_conta_com_senha_e_google_devolve_os_dois_na_ordem(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    usuario.google_sub = f"google-{uuid.uuid4().hex}"
    db_session.commit()

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["metodos_login"] == ["senha", "google"]


def test_resposta_nao_expoe_papel_nem_hashes(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    usuario.google_sub = f"google-{uuid.uuid4().hex}"
    db_session.commit()

    response = _me(cabecalho_de(usuario))

    corpo = response.json()
    assert "papel" not in corpo
    assert "senha_hash" not in corpo
    assert "google_sub" not in corpo
    assert usuario.senha_hash not in response.text
    assert usuario.google_sub not in response.text


# --- autenticação por usuário -----------------------------------------------


def test_sem_token_e_401():
    assert _me({}).status_code == 401


def test_token_assinado_com_outro_segredo_e_401(db_session, criar_usuario, gerar_token):
    usuario = criar_usuario()
    token = gerar_token(usuario.empresa_id, secret="outro-segredo-qualquer", usuario_id=usuario.id)

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_sub_que_nao_e_uuid_e_401(db_session, criar_empresa, gerar_token):
    token = gerar_token(criar_empresa().id, usuario_id="nao-e-uuid")

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_sub_de_usuario_inexistente_e_401(db_session, criar_empresa, gerar_token):
    empresa = criar_empresa()
    token = gerar_token(empresa.id, usuario_id=uuid.uuid4())

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_usuario_com_empresa_diferente_da_do_token_e_401(
    db_session, criar_usuario, criar_empresa, cabecalho_de
):
    usuario = criar_usuario()
    outra_empresa = criar_empresa()

    response = _me(cabecalho_de(usuario, empresa_id=outra_empresa.id))

    assert response.status_code == 401


# --- isolamento entre empresas ----------------------------------------------


def test_cada_usuario_recebe_so_os_dados_da_propria_empresa(
    db_session, criar_usuario, cabecalho_de
):
    usuario_a, usuario_b = criar_usuario(), criar_usuario()

    corpo_a = _me(cabecalho_de(usuario_a)).json()
    corpo_b = _me(cabecalho_de(usuario_b)).json()

    assert corpo_a["empresa_id"] != corpo_b["empresa_id"]
    assert corpo_a["razao_social"] == usuario_a.empresa.razao_social
    assert corpo_b["razao_social"] == usuario_b.empresa.razao_social


def test_token_com_sub_de_a_e_empresa_de_b_e_401(db_session, criar_usuario, cabecalho_de):
    usuario_a, usuario_b = criar_usuario(), criar_usuario()

    response = _me(cabecalho_de(usuario_a, empresa_id=usuario_b.empresa_id))

    assert response.status_code == 401


# --- rate limit por usuário --------------------------------------------------


def test_rate_limit_por_usuario_nao_afeta_outro_usuario(db_session, criar_usuario, cabecalho_de):
    alvo, outro = criar_usuario(), criar_usuario()

    respostas = [_me(cabecalho_de(alvo)) for _ in range(LIMITE + 1)]

    assert respostas[-2].status_code == 200
    assert respostas[-1].status_code == 429
    assert _me(cabecalho_de(outro)).status_code == 200
