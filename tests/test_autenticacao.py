"""Testes de autenticação (issue #14): JWT HS256 com claim empresa_id,
isolamento entre empresas (BOLA/IDOR) e rate limit do upload.

Tokens gerados com pyjwt (fixture `gerar_token`), simulando o NextAuth.
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

EMPRESA_ID = uuid.uuid4()
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


def test_token_com_assinatura_invalida_retorna_401(gerar_token):
    token = gerar_token(EMPRESA_ID, secret="outro-secret-com-32-bytes-ou-mais-ok")
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_expirado_retorna_401(gerar_token):
    token = gerar_token(EMPRESA_ID, expira_em=timedelta(seconds=-10))
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_sem_empresa_id_retorna_401(gerar_token):
    token = gerar_token(None)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_com_empresa_id_invalido_retorna_401(gerar_token):
    token = gerar_token("nao-e-uuid")
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_secret_vazio_falha_explicitamente_em_vez_de_validar(gerar_token, monkeypatch):
    token = gerar_token(EMPRESA_ID)
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


def test_upload_acima_de_10_por_minuto_retorna_429_na_11a(auth_headers):
    headers = auth_headers(EMPRESA_ID)

    # extensão inválida: responde 400 sem tocar no banco, mas conta no limite
    codigos = [_upload(headers, nome="extrato.pdf").status_code for _ in range(11)]

    assert codigos[:10] == [400] * 10
    assert codigos[10] == 429


def _upload_pelo_proxy(headers, x_forwarded_for):
    return _upload({**headers, "X-Forwarded-For": x_forwarded_for}, nome="extrato.pdf")


def test_rate_limit_separa_clientes_pelo_ip_anotado_pelo_proxy(auth_headers):
    headers = auth_headers(EMPRESA_ID)
    for _ in range(10):
        _upload_pelo_proxy(headers, "203.0.113.10")

    assert _upload_pelo_proxy(headers, "203.0.113.10").status_code == 429
    assert _upload_pelo_proxy(headers, "203.0.113.20").status_code == 400


def test_rate_limit_ignora_ip_forjado_pelo_cliente_no_x_forwarded_for(auth_headers):
    headers = auth_headers(EMPRESA_ID)

    # O cliente inventa o primeiro IP a cada envio; o proxy anota o real no fim.
    codigos = [
        _upload_pelo_proxy(headers, f"10.0.0.{i}, 203.0.113.10").status_code for i in range(11)
    ]

    assert codigos[10] == 429
