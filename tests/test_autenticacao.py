"""Testes de autenticação (issue #14): JWT HS256 com claim empresa_id,
isolamento entre empresas (BOLA/IDOR) e rate limit do upload.

Tokens gerados com pyjwt (fixture `gerar_token`), simulando o NextAuth.
Desde a issue #75, cada token é de um usuário real (`usuario`), para o 401
de cada teste vir do defeito testado e não de um `sub` inexistente. Os casos
de `auth_time` e de sessão encerrada estão em tests/test_revogacao_sessao.py.
"""

import io
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models import Extrato
from main import app

client = TestClient(app)


@pytest.fixture
def usuario(criar_usuario):
    return criar_usuario(com_senha=False)


CSV = b"data,valor,descricao\n2026-09-05,1500.00,Deposito ok\n"


def _upload(headers=None, nome="extrato.csv", conteudo=CSV):
    return client.post(
        "/extratos/upload",
        headers=headers or {},
        data={"origem": "banco"},
        files={"arquivo": (nome, io.BytesIO(conteudo), "text/csv")},
    )


def test_upload_sem_authorization_retorna_401():
    assert _upload().status_code == 401


def test_get_sem_authorization_retorna_401():
    assert client.get(f"/extratos/{uuid.uuid4()}").status_code == 401


def test_token_com_assinatura_invalida_retorna_401(usuario, gerar_token):
    token = gerar_token(
        usuario.empresa_id, secret="outro-secret-com-32-bytes-ou-mais-ok", usuario_id=usuario.id
    )
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_expirado_retorna_401(usuario, gerar_token):
    token = gerar_token(usuario.empresa_id, expira_em=timedelta(seconds=-10), usuario_id=usuario.id)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_sem_empresa_id_retorna_401(usuario, gerar_token):
    token = gerar_token(None, usuario_id=usuario.id)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_com_empresa_id_invalido_retorna_401(usuario, gerar_token):
    token = gerar_token("nao-e-uuid", usuario_id=usuario.id)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_secret_vazio_falha_explicitamente_em_vez_de_validar(usuario, gerar_token, monkeypatch):
    token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
    monkeypatch.setattr(settings, "nextauth_secret", "")
    with pytest.raises(RuntimeError, match="NEXTAUTH_SECRET"):
        TestClient(app).post(
            "/extratos/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={"origem": "banco"},
            files={"arquivo": ("extrato.csv", io.BytesIO(CSV), "text/csv")},
        )


def test_upload_usa_empresa_id_do_token(db_session, criar_empresa, auth_headers):
    empresa = criar_empresa()

    response = _upload(auth_headers(empresa.id))

    assert response.status_code == 201
    extrato = db_session.get(Extrato, uuid.UUID(response.json()["extrato_id"]))
    assert extrato.empresa_id == empresa.id


def test_get_com_token_valido_da_mesma_empresa_funciona(db_session, criar_empresa, auth_headers):
    empresa = criar_empresa()
    extrato_id = _upload(auth_headers(empresa.id)).json()["extrato_id"]

    response = client.get(f"/extratos/{extrato_id}", headers=auth_headers(empresa.id))

    assert response.status_code == 200


def test_get_extrato_de_outra_empresa_retorna_404(db_session, criar_empresa, auth_headers):
    dona = criar_empresa()
    outra = criar_empresa()
    extrato_id = _upload(auth_headers(dona.id)).json()["extrato_id"]

    response = client.get(f"/extratos/{extrato_id}", headers=auth_headers(outra.id))

    assert response.status_code == 404


def test_upload_acima_de_10_por_minuto_retorna_429_na_11a(criar_empresa, auth_headers):
    headers = auth_headers(criar_empresa().id)

    # extensão inválida: responde 400 sem gravar nada, mas conta no limite
    codigos = [_upload(headers, nome="extrato.pdf").status_code for _ in range(11)]

    assert codigos[:10] == [400] * 10
    assert codigos[10] == 429
