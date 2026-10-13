"""`POST /convites/aceitar/google` (issue #73): o aceite do convite da #69 com
o `id_token` do Google no lugar da senha.

Nenhum teste chama o Google nem o Resend: a fixture `google` troca o cliente
de chaves por um falso, e o convite sai pelo provedor de e-mail falso.
"""

import logging
import math
import re
import time
import uuid
from datetime import timedelta

import pytest
from conftest import FRONTEND_URL_TESTE as FRONTEND_URL
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.models import Convite, Usuario
from main import app

client = TestClient(app)

CONVITE_INVALIDO = {"detail": "Convite inválido ou expirado. Peça um novo convite."}


@pytest.fixture
def admin(db_session, criar_usuario):
    return criar_usuario(nome="Ana Administradora")


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


@pytest.fixture
def convidar(provedor, admin, cabecalho_de):
    """Convida o e-mail pela rota do administrador e devolve o token do link."""

    def _convidar(email: str) -> str:
        response = client.post(
            "/empresa/convites", json={"email": email}, headers=cabecalho_de(admin)
        )
        assert response.status_code == 201, response.text
        link = re.search(r"https://\S+", provedor.enviadas[-1].texto).group(0)
        assert link.startswith(f"{FRONTEND_URL}/aceitar-convite#token=")
        return link.split("#token=", 1)[1]

    return _convidar


def _email_novo() -> str:
    return f"{uuid.uuid4().hex[:12]}@gmail.com"


def _aceitar(token: str, id_token: str, **extra):
    return client.post(
        "/convites/aceitar/google", json={"token": token, "id_token": id_token, **extra}
    )


def _convite(db_session, admin) -> Convite:
    db_session.expire_all()
    return db_session.scalars(select(Convite).where(Convite.empresa_id == admin.empresa_id)).one()


def test_aceite_com_o_mesmo_email_cria_o_usuario_sem_senha(
    db_session, google, admin, convidar, gerar_token
):
    email = _email_novo()
    token = convidar(email)
    antes = time.time()

    response = _aceitar(token, google.id_token(email=email.upper(), sub="20000000000000000001"))

    assert response.status_code == 201
    corpo = response.json()
    assert set(corpo) == {"id", "empresa_id", "nome", "email", "auth_time"}
    assert corpo["empresa_id"] == str(admin.empresa_id)
    assert corpo["email"] == email
    assert corpo["nome"] == "Maria Google"
    assert math.floor(antes) <= corpo["auth_time"] <= time.time()

    usuario = db_session.get(Usuario, uuid.UUID(corpo["id"]))
    assert usuario.senha_hash is None
    assert usuario.google_sub == "20000000000000000001"
    assert usuario.administrador is False
    assert _convite(db_session, admin).usado_em is not None

    # O token montado com a resposta passa, e o login pelo Google acha a conta.
    cabecalho = {
        "Authorization": "Bearer "
        + gerar_token(admin.empresa_id, usuario_id=usuario.id, auth_time=corpo["auth_time"])
    }
    me = client.get("/me", headers=cabecalho)
    assert me.status_code == 200
    assert me.json()["metodos_login"] == ["google"]
    assert me.json()["administrador"] is False
    login = client.post(
        "/login/google", json={"id_token": google.id_token(email=email, sub=usuario.google_sub)}
    )
    assert login.json()["id"] == corpo["id"]


def test_aceite_usa_o_nome_do_corpo_quando_vem(db_session, google, convidar):
    email = _email_novo()

    response = _aceitar(convidar(email), google.id_token(email=email), nome="  Joana  ")

    assert response.json()["nome"] == "Joana"


def test_aceite_sem_nome_no_token_nem_no_corpo_e_422_e_nao_consome(
    db_session, google, admin, convidar
):
    email = _email_novo()

    response = _aceitar(convidar(email), google.id_token(email=email, nome=None))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "nome"]
    assert _convite(db_session, admin).usado_em is None


def test_email_do_google_diferente_do_convidado_e_409_e_nao_consome(
    db_session, google, admin, convidar
):
    token = convidar(_email_novo())

    response = _aceitar(token, google.id_token(email=_email_novo()))

    assert response.status_code == 409
    assert response.json() == {
        "detail": "O e-mail da conta Google é diferente do e-mail convidado."
    }
    assert _convite(db_session, admin).usado_em is None
    assert (
        db_session.scalar(select(func.count()).where(Usuario.empresa_id == admin.empresa_id)) == 1
    )


def test_convite_usado_e_400(db_session, google, convidar):
    email = _email_novo()
    token = convidar(email)
    assert _aceitar(token, google.id_token(email=email)).status_code == 201

    response = _aceitar(token, google.id_token(email=email))

    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO


def test_convite_vencido_e_400(db_session, google, admin, convidar):
    email = _email_novo()
    token = convidar(email)
    db_session.execute(
        update(Convite)
        .where(Convite.empresa_id == admin.empresa_id)
        .values(expira_em=func.now() - timedelta(minutes=1))
    )
    db_session.commit()

    response = _aceitar(token, google.id_token(email=email))

    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO


def test_convite_cancelado_e_400(db_session, google, admin, convidar, cabecalho_de):
    email = _email_novo()
    token = convidar(email)
    convite_id = _convite(db_session, admin).id
    assert (
        client.delete(f"/empresa/convites/{convite_id}", headers=cabecalho_de(admin)).status_code
        == 204
    )

    response = _aceitar(token, google.id_token(email=email))

    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO


def test_token_de_convite_inexistente_e_400(db_session, google):
    response = _aceitar("token-que-nao-existe", google.id_token())

    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO


def test_email_que_ganhou_conta_no_meio_tempo_e_409(
    db_session, google, admin, convidar, criar_usuario
):
    email = _email_novo()
    token = convidar(email)
    outro = criar_usuario()
    outro.email = email
    db_session.commit()

    response = _aceitar(token, google.id_token(email=email))

    assert response.status_code == 409
    assert response.json() == {"detail": "E-mail já cadastrado."}
    assert _convite(db_session, admin).usado_em is None


def test_conta_google_que_ja_entra_com_outro_email_e_409(
    db_session, google, convidar, criar_usuario
):
    """A pessoa trocou o e-mail no Google: o `sub` já tem conta no Ledgr."""
    ja_cadastrado = criar_usuario(com_senha=False)
    email = _email_novo()

    response = _aceitar(convidar(email), google.id_token(email=email, sub=ja_cadastrado.google_sub))

    assert response.status_code == 409
    assert response.json() == {"detail": "Já existe uma conta no Ledgr para esta conta Google."}


def test_logs_nao_tem_token_id_token_email_nem_sub(db_session, google, convidar, caplog):
    caplog.set_level(logging.DEBUG)
    email = _email_novo()
    token = convidar(email)
    id_token_errado = google.id_token(email=_email_novo(), sub="30000000000000000003")
    id_token = google.id_token(email=email, sub="30000000000000000004")

    _aceitar(token, id_token_errado)
    _aceitar(token, id_token)

    assert "convite_aceite_google resultado=email_diferente" in caplog.text
    assert "convite_aceite_google resultado=aceito" in caplog.text
    for texto in [token, id_token, id_token_errado, email, "300000000000000000", "@gmail.com"]:
        assert texto not in caplog.text
