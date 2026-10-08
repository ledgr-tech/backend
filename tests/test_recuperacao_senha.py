"""Testes de POST /senha/recuperar e POST /senha/redefinir (issue #66,
ADR-012).

O provedor de e-mail entra por dependência e é trocado por um falso, que
só guarda as mensagens: nenhum teste chama o Resend. O `TestClient` roda as
`BackgroundTasks` antes de devolver a resposta, então o envio já aconteceu
quando o teste confere o resultado.
"""

import hashlib
import logging
import re
import uuid

import pytest
from conftest import FRONTEND_URL_TESTE as FRONTEND_URL
from conftest import SENHA_USUARIO_TESTE as SENHA_ATUAL
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.senha import LIMITE_ENVIOS_POR_HORA, obter_provedor_email_dependencia
from app.core.config import settings
from app.models import TokenEmail
from app.services.email import MensagemEmail, ProvedorEmailIndisponivel
from main import app

client = TestClient(app)

SENHA_NOVA = "senha-nova-456"
RESPOSTA_PEDIDO = {
    "mensagem": "Se o e-mail estiver cadastrado, enviaremos um link para redefinir a senha."
}


def _pedir(email: str):
    return client.post("/senha/recuperar", json={"email": email})


def _redefinir(token: str, senha_nova: str = SENHA_NOVA):
    return client.post("/senha/redefinir", json={"token": token, "senha_nova": senha_nova})


def _token_do_link(mensagem: MensagemEmail) -> str:
    link = re.search(r"https://\S+", mensagem.texto).group(0)
    assert link.startswith(f"{FRONTEND_URL}/redefinir-senha#token=")
    return link.split("#token=", 1)[1]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- pedido de recuperação -------------------------------------------------


def test_pedido_envia_link_para_o_email_cadastrado(db_session, provedor, criar_usuario):
    usuario = criar_usuario()

    response = _pedir(usuario.email)

    assert response.status_code == 202
    assert response.json() == RESPOSTA_PEDIDO
    (mensagem,) = provedor.enviadas
    assert mensagem.para == usuario.email
    assert mensagem.chave_idempotencia
    token = _token_do_link(mensagem)
    assert len(token) >= 43


def test_token_e_guardado_so_como_hash(db_session, provedor, criar_usuario):
    usuario = criar_usuario()

    _pedir(usuario.email)

    token = _token_do_link(provedor.enviadas[0])
    registro = db_session.scalar(select(TokenEmail).where(TokenEmail.usuario_id == usuario.id))
    assert registro.token_hash == _hash(token)
    assert registro.finalidade == "recuperacao_senha"
    assert registro.usado_em is None
    colunas = db_session.execute(
        text("SELECT * FROM tokens_email WHERE id = :id"), {"id": registro.id}
    ).one()
    assert token not in [str(valor) for valor in colunas]


def test_pedido_normaliza_o_email(db_session, provedor, criar_usuario):
    usuario = criar_usuario()

    _pedir(f"  {usuario.email.upper()} ")

    assert len(provedor.enviadas) == 1


def test_email_inexistente_tem_a_mesma_resposta_e_nao_envia(db_session, provedor):
    response = _pedir(f"{uuid.uuid4().hex}@ninguem.com")

    assert response.status_code == 202
    assert response.json() == RESPOSTA_PEDIDO
    assert provedor.enviadas == []


def test_conta_google_sem_senha_tem_a_mesma_resposta_e_nao_envia(
    db_session, provedor, criar_usuario
):
    usuario = criar_usuario(com_senha=False)

    response = _pedir(usuario.email)

    assert response.status_code == 202
    assert response.json() == RESPOSTA_PEDIDO
    assert provedor.enviadas == []


def test_email_invalido_e_422(provedor):
    response = _pedir("nao-e-email")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "email"]


def test_sem_provedor_configurado_e_503(monkeypatch):
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: None
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL)
    try:
        response = _pedir("alguem@teste.com")
    finally:
        app.dependency_overrides.pop(obter_provedor_email_dependencia, None)

    assert response.status_code == 503


def test_sem_frontend_url_e_503(provedor, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "")

    response = _pedir("alguem@teste.com")

    assert response.status_code == 503
    assert provedor.enviadas == []


def test_limite_de_envios_por_destino_nao_muda_a_resposta(db_session, provedor, criar_usuario):
    usuario = criar_usuario()

    respostas = [_pedir(usuario.email) for _ in range(LIMITE_ENVIOS_POR_HORA + 1)]

    assert {r.status_code for r in respostas} == {202}
    assert len(provedor.enviadas) == LIMITE_ENVIOS_POR_HORA


def test_pedido_novo_invalida_o_link_anterior(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    _pedir(usuario.email)
    _pedir(usuario.email)
    primeiro, segundo = (_token_do_link(m) for m in provedor.enviadas)

    assert _redefinir(primeiro).status_code == 400
    assert _redefinir(segundo).status_code == 204


def test_falha_do_provedor_nao_muda_a_resposta_nem_vaza_no_log(
    db_session, provedor, criar_usuario, caplog
):
    caplog.set_level(logging.DEBUG)
    provedor._falha = ProvedorEmailIndisponivel("http", status_http=500)
    usuario = criar_usuario()

    response = _pedir(usuario.email)

    assert response.status_code == 202
    assert response.json() == RESPOSTA_PEDIDO
    assert "motivo=http" in caplog.text
    assert usuario.email not in caplog.text
    assert "#token=" not in caplog.text


def test_rate_limit_por_ip_no_pedido(provedor):
    respostas = [_pedir(f"{uuid.uuid4().hex}@ninguem.com") for _ in range(11)]

    assert respostas[-1].status_code == 429


# --- redefinição --------------------------------------------------------------


def _token_valido(provedor, usuario) -> str:
    _pedir(usuario.email)
    return _token_do_link(provedor.enviadas[-1])


def test_redefinir_troca_a_senha(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    token = _token_valido(provedor, usuario)

    response = _redefinir(token)

    assert response.status_code == 204
    login_novo = client.post("/login", json={"email": usuario.email, "senha": SENHA_NOVA})
    login_antigo = client.post("/login", json={"email": usuario.email, "senha": SENHA_ATUAL})
    assert login_novo.status_code == 200
    assert login_antigo.status_code == 401


def test_token_reutilizado_e_400(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    token = _token_valido(provedor, usuario)
    _redefinir(token)

    response = _redefinir(token, senha_nova="outra-senha-789")

    assert response.status_code == 400
    assert response.json() == {"detail": "Link inválido ou expirado. Peça um novo."}


def test_token_expirado_e_400(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    token = _token_valido(provedor, usuario)
    db_session.execute(
        text(
            "UPDATE tokens_email SET expira_em = now() - interval '1 minute' WHERE token_hash = :h"
        ),
        {"h": _hash(token)},
    )
    db_session.commit()

    assert _redefinir(token).status_code == 400


def test_token_inexistente_e_400_nunca_401(db_session):
    response = _redefinir("token-que-nao-existe")

    assert response.status_code == 400


def test_token_de_outra_finalidade_nao_redefine(db_session, criar_usuario):
    usuario = criar_usuario()
    token = "token-de-troca-de-email"
    db_session.add(
        TokenEmail(
            usuario_id=usuario.id,
            finalidade="troca_email",
            token_hash=_hash(token),
            expira_em=db_session.scalar(text("SELECT now() + interval '30 minutes'")),
        )
    )
    db_session.commit()

    assert _redefinir(token).status_code == 400


@pytest.mark.parametrize("senha_nova", ["curta", "ç" * 37])
def test_senha_nova_fora_das_regras_e_422(senha_nova):
    response = _redefinir("qualquer-token", senha_nova=senha_nova)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "senha_nova"]


def test_redefinir_envia_aviso_de_senha_alterada(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    token = _token_valido(provedor, usuario)

    _redefinir(token)

    aviso = provedor.enviadas[-1]
    assert aviso.para == usuario.email
    assert "alterada" in aviso.assunto.lower()
    assert token not in aviso.texto


def test_token_invalido_nao_envia_aviso(db_session, provedor):
    _redefinir("token-que-nao-existe")

    assert provedor.enviadas == []
