"""Testes de autenticação (issue #14): JWT HS256 com o claim do tenant,
isolamento entre empresas (BOLA/IDOR) e rate limit do upload.

Desde a issue #133 (ADR-019, vetor v3 do ADR-005), o claim é `conta_id`; o
legado `empresa_id` continua aceito quando `conta_id` não vem, e os dois são
lidos como o id da conta. Os dois juntos e diferentes: 401. As regras da
empresa ativa (`X-Ledgr-Empresa`) estão em tests/test_empresa_ativa.py.

Tokens gerados com pyjwt (fixture `gerar_token`), simulando o NextAuth.
Desde a issue #75, cada token é de um usuário real (`usuario`), para o 401
de cada teste vir do defeito testado e não de um `sub` inexistente. Os casos
de `auth_time` e de sessão encerrada estão em tests/test_revogacao_sessao.py.
"""

import io
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import SECRET_TESTE
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
        usuario.conta_id, secret="outro-secret-com-32-bytes-ou-mais-ok", usuario_id=usuario.id
    )
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_expirado_retorna_401(usuario, gerar_token):
    token = gerar_token(usuario.conta_id, expira_em=timedelta(seconds=-10), usuario_id=usuario.id)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_sem_conta_id_nem_empresa_id_retorna_401(usuario, gerar_token):
    token = gerar_token(None, usuario_id=usuario.id)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("claim", ["conta_id", "empresa_id"])
def test_token_com_id_da_conta_invalido_retorna_401(usuario, gerar_token, claim):
    token = gerar_token("nao-e-uuid", usuario_id=usuario.id, claim=claim)
    assert _upload({"Authorization": f"Bearer {token}"}).status_code == 401


def _token(usuario, **tenant) -> dict[str, str]:
    agora = datetime.now(UTC)
    claims = {
        "sub": str(usuario.id),
        "email": "usuario@teste.com",
        "iat": agora,
        "exp": agora + timedelta(hours=1),
        "auth_time": int(agora.timestamp()),
        **{nome: str(valor) for nome, valor in tenant.items()},
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET_TESTE, algorithm='HS256')}"}


@pytest.mark.parametrize(
    "claims",
    [["conta_id"], ["empresa_id"], ["conta_id", "empresa_id"]],
    ids=["conta_id", "legado_empresa_id", "os_dois_iguais"],
)
def test_token_com_conta_id_ou_com_o_legado_e_aceito(db_session, usuario, claims):
    """O front de antes da #133 manda `empresa_id` com o valor que o login
    devolvia, que a migração fez virar o id da conta."""
    headers = _token(usuario, **dict.fromkeys(claims, usuario.conta_id))

    resposta = client.get("/me", headers=headers)

    assert resposta.status_code == 200
    assert resposta.json()["conta_id"] == str(usuario.conta_id)
    assert _upload(headers).status_code == 201


def test_token_com_conta_id_e_empresa_id_divergentes_retorna_401(
    db_session, usuario, criar_usuario
):
    outra_conta = criar_usuario().conta_id

    for conta_id, empresa_id in [(usuario.conta_id, outra_conta), (outra_conta, usuario.conta_id)]:
        headers = _token(usuario, conta_id=conta_id, empresa_id=empresa_id)
        assert client.get("/me", headers=headers).status_code == 401
        assert _upload(headers).status_code == 401


def test_legado_com_o_id_da_empresa_cliente_retorna_401(db_session, usuario):
    """Só o id da conta vale no claim, nunca o de uma empresa cliente dela."""
    (empresa,) = usuario.conta.empresas

    assert client.get("/me", headers=_token(usuario, empresa_id=empresa.id)).status_code == 401


def test_secret_vazio_falha_explicitamente_em_vez_de_validar(usuario, gerar_token, monkeypatch):
    token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
    monkeypatch.setattr(settings, "nextauth_secret", "")
    with pytest.raises(RuntimeError, match="NEXTAUTH_SECRET"):
        TestClient(app).post(
            "/extratos/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={"origem": "banco"},
            files={"arquivo": ("extrato.csv", io.BytesIO(CSV), "text/csv")},
        )


def test_upload_usa_a_empresa_ativa(db_session, criar_empresa, auth_headers):
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
