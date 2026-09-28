"""Testes de POST /me/senha (issue #66, ADR-012): troca de senha de quem está
logado, com aviso por e-mail.

A rota é a primeira por usuário: lê o `sub` do token, além do `empresa_id`.
O provedor de e-mail é o falso do conftest.py; o `TestClient` roda as
`BackgroundTasks` antes de devolver a resposta.
"""

import logging
import re
import uuid

import pytest
from conftest import SENHA_USUARIO_TESTE as SENHA_ATUAL
from fastapi.testclient import TestClient

from app.api.me import LIMITE_TROCA_SENHA_POR_USUARIO
from app.api.senha import obter_provedor_email_dependencia
from app.services.email import ProvedorEmailIndisponivel
from main import app

client = TestClient(app)

SENHA_NOVA = "senha-nova-456"
LIMITE = int(LIMITE_TROCA_SENHA_POR_USUARIO.split("/")[0])


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario, empresa_id=None) -> dict[str, str]:
        token = gerar_token(empresa_id or usuario.empresa_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _trocar(cabecalho, senha_atual=SENHA_ATUAL, senha_nova=SENHA_NOVA):
    return client.post(
        "/me/senha",
        json={"senha_atual": senha_atual, "senha_nova": senha_nova},
        headers=cabecalho,
    )


def _login(email, senha):
    return client.post("/login", json={"email": email, "senha": senha})


# --- sucesso -------------------------------------------------------------


def test_troca_a_senha_na_hora(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    response = _trocar(cabecalho_de(usuario))

    assert response.status_code == 204
    assert _login(usuario.email, SENHA_NOVA).status_code == 200
    assert _login(usuario.email, SENHA_ATUAL).status_code == 401


def test_troca_envia_aviso_de_senha_alterada(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    _trocar(cabecalho_de(usuario))

    (aviso,) = provedor.enviadas
    assert aviso.para == usuario.email
    assert "alterada" in aviso.assunto.lower()
    assert "Esqueci a senha" in aviso.texto
    for segredo in (SENHA_NOVA, SENHA_ATUAL):
        assert segredo not in aviso.texto
        assert segredo not in aviso.html


def test_troca_funciona_com_email_desligado(db_session, criar_usuario, cabecalho_de):
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: None
    try:
        usuario = criar_usuario()
        response = _trocar(cabecalho_de(usuario))
    finally:
        app.dependency_overrides.pop(obter_provedor_email_dependencia, None)

    assert response.status_code == 204
    assert _login(usuario.email, SENHA_NOVA).status_code == 200


def test_falha_no_aviso_nao_desfaz_a_troca(
    db_session, provedor, criar_usuario, cabecalho_de, caplog
):
    caplog.set_level(logging.INFO)
    provedor._falha = ProvedorEmailIndisponivel("timeout")
    usuario = criar_usuario()

    response = _trocar(cabecalho_de(usuario))

    assert response.status_code == 204
    assert _login(usuario.email, SENHA_NOVA).status_code == 200
    assert "aviso_senha_alterada resultado=falha_envio motivo=timeout" in caplog.text
    assert usuario.email not in caplog.text


def test_troca_invalida_link_de_recuperacao_pendente(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    client.post("/senha/recuperar", json={"email": usuario.email})
    link = re.search(r"#token=(\S+)", provedor.enviadas[0].texto).group(1)

    _trocar(cabecalho_de(usuario))
    response = client.post("/senha/redefinir", json={"token": link, "senha_nova": "outra-9876"})

    assert response.status_code == 400


# --- recusas ---------------------------------------------------------------


def test_senha_atual_incorreta_e_400_nunca_401(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    response = _trocar(cabecalho_de(usuario), senha_atual="senha-errada")

    assert response.status_code == 400
    assert response.json() == {"detail": "Senha atual incorreta."}
    assert provedor.enviadas == []
    assert _login(usuario.email, SENHA_ATUAL).status_code == 200


def test_conta_google_sem_senha_e_409(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)

    response = _trocar(cabecalho_de(usuario))

    assert response.status_code == 409
    assert response.json() == {"detail": "Esta conta entra pelo Google e não tem senha."}


@pytest.mark.parametrize("senha_nova", ["curta", "ç" * 37])
def test_senha_nova_fora_das_regras_e_422(
    db_session, provedor, criar_usuario, cabecalho_de, senha_nova
):
    usuario = criar_usuario()

    response = _trocar(cabecalho_de(usuario), senha_nova=senha_nova)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "senha_nova"]


# --- autenticação por usuário ---------------------------------------------


def test_sem_token_e_401():
    assert _trocar({}).status_code == 401


def test_sub_de_usuario_inexistente_e_401(db_session, criar_empresa, gerar_token):
    empresa = criar_empresa()
    token = gerar_token(empresa.id, usuario_id=uuid.uuid4())

    assert _trocar({"Authorization": f"Bearer {token}"}).status_code == 401


def test_sub_que_nao_e_uuid_e_401(db_session, criar_empresa, gerar_token):
    token = gerar_token(criar_empresa().id, usuario_id="nao-e-uuid")

    assert _trocar({"Authorization": f"Bearer {token}"}).status_code == 401


def test_usuario_com_empresa_diferente_da_do_token_e_401(
    db_session, criar_usuario, criar_empresa, cabecalho_de
):
    usuario = criar_usuario()
    outra_empresa = criar_empresa()

    response = _trocar(cabecalho_de(usuario, empresa_id=outra_empresa.id))

    assert response.status_code == 401


# --- rate limit por usuário -------------------------------------------------


def test_rate_limit_por_usuario_nao_afeta_outro_usuario(
    db_session, provedor, criar_usuario, cabecalho_de
):
    alvo, outro = criar_usuario(), criar_usuario()

    respostas = [_trocar(cabecalho_de(alvo), senha_atual="chute-errado") for _ in range(LIMITE + 1)]

    assert respostas[-2].status_code == 400
    assert respostas[-1].status_code == 429
    assert _trocar(cabecalho_de(outro)).status_code == 204
