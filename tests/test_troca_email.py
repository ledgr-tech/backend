"""Testes de POST /me/email e POST /me/email/confirmar (issue #149; ADR-012,
adendo de 22/10).

O provedor de e-mail é o falso da fixture `provedor`: nenhum teste manda
e-mail de verdade. O `TestClient` roda as `BackgroundTasks` antes de devolver
a resposta, então os envios já aconteceram quando o teste confere.
"""

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

import pytest
from conftest import FRONTEND_URL_TESTE as FRONTEND_URL
from conftest import SENHA_USUARIO_TESTE as SENHA
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.troca_email import LIMITE_PEDIDOS_POR_HORA, VALIDADE_TOKEN_MINUTOS
from app.models import TokenEmail, Usuario
from app.models.token_email import FINALIDADE_RECUPERACAO_SENHA, FINALIDADE_TROCA_EMAIL
from app.services.email.mensagens import mascarar_email
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)


@pytest.fixture
def cabecalho_de(gerar_token):
    """Token de uma sessão aberta há um minuto: a confirmação, que encerra as
    sessões no segundo atual, derruba esse token."""

    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(
            usuario.conta_id, usuario_id=usuario.id, auth_time=int(time.time()) - 60
        )
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _email_novo() -> str:
    return f"novo-{uuid.uuid4().hex[:10]}@teste.com"


def _pedir(cabecalho, email_novo, senha_atual=SENHA):
    corpo = {"email_novo": email_novo}
    if senha_atual is not None:
        corpo["senha_atual"] = senha_atual
    return client.post("/me/email", headers=cabecalho, json=corpo)


def _confirmar(token):
    return client.post("/me/email/confirmar", json={"token": token})


def _token_do_link(mensagem) -> str:
    link = re.search(r"https://\S+", mensagem.texto).group(0)
    assert link.startswith(f"{FRONTEND_URL}/confirmar-email#token=")
    return link.split("#token=", 1)[1]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens(db_session, usuario, finalidade=FINALIDADE_TROCA_EMAIL):
    db_session.expire_all()
    return db_session.scalars(
        select(TokenEmail)
        .where(TokenEmail.usuario_id == usuario.id, TokenEmail.finalidade == finalidade)
        .order_by(TokenEmail.criado_em)
    ).all()


def _email_do_banco(db_session, usuario) -> str:
    db_session.expire_all()
    return db_session.get(Usuario, usuario.id).email


# --- pedido ---------------------------------------------------------------------------


def test_pedido_valido_manda_link_ao_novo_e_aviso_ao_atual_sem_trocar(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    email_atual, email_novo = usuario.email, _email_novo()

    antes = datetime.now().astimezone()
    resposta = _pedir(cabecalho_de(usuario), f"  {email_novo.upper()} ")

    assert resposta.status_code == 202
    corpo = resposta.json()
    assert corpo["email_novo"] == email_novo
    expira_em = datetime.fromisoformat(corpo["expira_em"])
    assert (
        timedelta(minutes=VALIDADE_TOKEN_MINUTOS - 1)
        < expira_em - antes
        < timedelta(minutes=VALIDADE_TOKEN_MINUTOS + 1)
    )
    assert _email_do_banco(db_session, usuario) == email_atual

    link, aviso = provedor.enviadas
    assert link.para == email_novo
    assert aviso.para == email_atual
    assert mascarar_email(email_novo) in aviso.texto
    assert email_novo not in aviso.texto and email_novo not in aviso.html
    assert "troque a senha" in aviso.texto and "suporte" in aviso.texto
    assert link.chave_idempotencia and aviso.chave_idempotencia
    assert link.chave_idempotencia != aviso.chave_idempotencia

    token = _token_do_link(link)
    (registro,) = _tokens(db_session, usuario)
    assert registro.token_hash == _hash(token)
    assert registro.email_novo == email_novo and registro.usado_em is None
    assert str(registro.id) in link.chave_idempotencia
    valores = db_session.execute(
        text("SELECT * FROM tokens_email WHERE id = :id"), {"id": registro.id}
    ).one()
    assert token not in [str(v) for v in valores]


def test_mascara_do_email():
    assert mascarar_email("nome.sobrenome@dominio.com.br") == "n***@dominio.com.br"


def test_pedido_novo_cancela_o_pendente(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)

    _pedir(cabecalho, _email_novo())
    primeiro = _token_do_link(provedor.enviadas[0])
    _pedir(cabecalho, _email_novo())

    antigo, novo = _tokens(db_session, usuario)
    assert antigo.usado_em is not None and novo.usado_em is None
    resposta = _confirmar(primeiro)
    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Link inválido ou expirado."}


def test_conta_do_google_da_409(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)

    resposta = _pedir(cabecalho_de(usuario), _email_novo(), senha_atual=None)

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": "Esta conta entra pelo Google. O e-mail dela é o da conta Google."
    }
    assert provedor.enviadas == [] and _tokens(db_session, usuario) == []


@pytest.mark.parametrize("senha_atual", ["senha-errada", "", None])
def test_senha_errada_ou_ausente_da_400(
    db_session, provedor, criar_usuario, cabecalho_de, senha_atual
):
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), _email_novo(), senha_atual=senha_atual)

    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Senha atual incorreta."}
    assert provedor.enviadas == [] and _tokens(db_session, usuario) == []


def test_senha_errada_vem_antes_do_email_invalido(provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), "nao-e-email", senha_atual="errada")

    assert resposta.status_code == 400


@pytest.mark.parametrize("email", ["nao-e-email", "sem@dominio", "@teste.com", ""])
def test_email_invalido_da_422_no_formato_do_fastapi(provedor, criar_usuario, cabecalho_de, email):
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), email)

    assert resposta.status_code == 422
    (erro,) = resposta.json()["detail"]
    assert erro["loc"] == ["body", "email_novo"]


def test_email_igual_ao_atual_da_422(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), f" {usuario.email.upper()} ")

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": "O e-mail novo é igual ao atual."}
    assert _tokens(db_session, usuario) == []


def test_email_de_outro_usuario_da_409(db_session, provedor, criar_usuario, cabecalho_de):
    usuario, outro = criar_usuario(), criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), outro.email)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": "E-mail já cadastrado."}
    assert provedor.enviadas == [] and _tokens(db_session, usuario) == []


def test_email_desligado_da_503_sem_gravar(db_session, criar_usuario, cabecalho_de):
    """Sem a fixture `provedor`, o conftest deixa o e-mail desligado."""
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), _email_novo())

    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "Troca de e-mail indisponível no momento."}
    assert _tokens(db_session, usuario) == []


def test_frontend_url_recusada_da_503(
    db_session, provedor, criar_usuario, cabecalho_de, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "http://ledgr.vercel.app")
    usuario = criar_usuario()

    resposta = _pedir(cabecalho_de(usuario), _email_novo())

    assert resposta.status_code == 503
    assert _tokens(db_session, usuario) == [] and provedor.enviadas == []


def test_quarto_pedido_na_hora_da_429_com_retry_after(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)

    for _ in range(LIMITE_PEDIDOS_POR_HORA):
        assert _pedir(cabecalho, _email_novo()).status_code == 202
    resposta = _pedir(cabecalho, _email_novo())

    assert resposta.status_code == 429
    assert 3500 <= int(resposta.headers["retry-after"]) <= 3600
    assert isinstance(resposta.json()["detail"], str)
    assert len(_tokens(db_session, usuario)) == LIMITE_PEDIDOS_POR_HORA


def test_pedidos_de_mais_de_uma_hora_nao_contam(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    for _ in range(LIMITE_PEDIDOS_POR_HORA):
        _pedir(cabecalho, _email_novo())
    db_session.execute(
        text(
            "UPDATE tokens_email SET criado_em = criado_em - interval '61 minutes' WHERE usuario_id = :id"
        ),
        {"id": usuario.id},
    )
    db_session.commit()

    assert _pedir(cabecalho, _email_novo()).status_code == 202


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_todos_os_papeis_pedem(db_session, provedor, criar_usuario, cabecalho_de, papel):
    admin = criar_usuario()
    usuario = criar_usuario(conta_id=admin.conta_id, papel=papel)

    assert _pedir(cabecalho_de(usuario), _email_novo()).status_code == 202


def test_pedido_sem_sessao_da_401(provedor):
    assert _pedir({}, _email_novo()).status_code == 401


# --- confirmação ----------------------------------------------------------------------


def test_confirmacao_troca_o_email_encerra_sessoes_e_avisa_o_antigo(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    email_antigo, email_novo = usuario.email, _email_novo()
    _pedir(cabecalho, email_novo)
    token = _token_do_link(provedor.enviadas[0])
    provedor.enviadas.clear()

    resposta = _confirmar(token)

    assert resposta.status_code == 200
    assert resposta.json() == {"email": email_novo}
    assert _email_do_banco(db_session, usuario) == email_novo
    assert client.get("/me", headers=cabecalho).status_code == 401
    (aviso,) = provedor.enviadas
    assert aviso.para == email_antigo
    assert mascarar_email(email_novo) in aviso.texto
    assert aviso.chave_idempotencia
    login_novo = client.post("/login", json={"email": email_novo, "senha": SENHA})
    assert login_novo.status_code == 200
    assert client.post("/login", json={"email": email_antigo, "senha": SENHA}).status_code == 401
    assert _confirmar(token).status_code == 400  # uso único


def test_confirmacao_mata_os_outros_tokens_pendentes(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    assert client.post("/senha/recuperar", json={"email": usuario.email}).status_code == 202
    _pedir(cabecalho_de(usuario), _email_novo())
    token = _token_do_link(provedor.enviadas[-2])

    assert _confirmar(token).status_code == 200

    (recuperacao,) = _tokens(db_session, usuario, FINALIDADE_RECUPERACAO_SENHA)
    (troca,) = _tokens(db_session, usuario)
    assert recuperacao.usado_em is not None and troca.usado_em is not None


def test_confirmacao_nao_precisa_de_sessao(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    _pedir(cabecalho_de(usuario), _email_novo())
    token = _token_do_link(provedor.enviadas[0])

    resposta = client.post(
        "/me/email/confirmar", json={"token": token}, headers={"Authorization": "Bearer lixo"}
    )

    assert resposta.status_code == 200


def test_confirmacao_com_email_desligado_troca_sem_aviso(
    db_session, provedor, criar_usuario, cabecalho_de
):
    from app.api.senha import obter_provedor_email_dependencia

    usuario = criar_usuario()
    email_novo = _email_novo()
    _pedir(cabecalho_de(usuario), email_novo)
    token = _token_do_link(provedor.enviadas[0])
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: None

    assert _confirmar(token).status_code == 200
    assert _email_do_banco(db_session, usuario) == email_novo


@pytest.mark.parametrize("token", ["inexistente", "x" * 43])
def test_token_inexistente_da_400(provedor, token):
    resposta = _confirmar(token)

    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Link inválido ou expirado."}


def test_token_vazio_da_422(provedor):
    assert _confirmar("").status_code == 422


def test_token_expirado_da_400(db_session, provedor, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    email_atual = usuario.email
    _pedir(cabecalho_de(usuario), _email_novo())
    token = _token_do_link(provedor.enviadas[0])
    db_session.execute(
        text(
            "UPDATE tokens_email SET expira_em = now() - interval '1 second' WHERE usuario_id = :id"
        ),
        {"id": usuario.id},
    )
    db_session.commit()

    assert _confirmar(token).status_code == 400
    assert _email_do_banco(db_session, usuario) == email_atual


def test_token_de_recuperacao_de_senha_nao_confirma_troca(db_session, provedor, criar_usuario):
    usuario = criar_usuario()
    client.post("/senha/recuperar", json={"email": usuario.email})
    link = re.search(r"https://\S+", provedor.enviadas[0].texto).group(0)
    token = link.split("#token=", 1)[1]

    assert _confirmar(token).status_code == 400


def test_email_tomado_entre_o_pedido_e_a_confirmacao_da_409(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario, outro = criar_usuario(), criar_usuario()
    email_atual, email_novo = usuario.email, _email_novo()
    _pedir(cabecalho_de(usuario), email_novo)
    token = _token_do_link(provedor.enviadas[0])
    db_session.get(Usuario, outro.id).email = email_novo
    db_session.commit()

    resposta = _confirmar(token)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": "E-mail já cadastrado."}
    assert _email_do_banco(db_session, usuario) == email_atual
    (registro,) = _tokens(db_session, usuario)
    assert registro.usado_em is not None
    assert _confirmar(token).status_code == 400


def test_usuario_removido_entre_o_pedido_e_a_confirmacao_da_400(
    db_session, provedor, criar_usuario, cabecalho_de
):
    usuario = criar_usuario()
    _pedir(cabecalho_de(usuario), _email_novo())
    token = _token_do_link(provedor.enviadas[0])
    linha = db_session.get(Usuario, usuario.id)
    remover_usuario(db_session, linha)
    db_session.commit()

    assert _confirmar(token).status_code == 400


def test_removido_com_token_ainda_pendente_da_400(
    db_session, provedor, criar_usuario, cabecalho_de
):
    """Mesmo se o token tivesse escapado da invalidação da remoção."""
    usuario = criar_usuario()
    _pedir(cabecalho_de(usuario), _email_novo())
    token = _token_do_link(provedor.enviadas[0])
    db_session.execute(
        text(
            "UPDATE usuarios SET removido_em = now(), senha_hash = NULL, google_sub = NULL "
            "WHERE id = :id"
        ),
        {"id": usuario.id},
    )
    db_session.commit()

    assert _confirmar(token).status_code == 400


# --- logs -----------------------------------------------------------------------------


def test_logs_nao_tem_email_token_nem_link(
    db_session, provedor, criar_usuario, cabecalho_de, caplog
):
    usuario = criar_usuario()
    email_novo = _email_novo()
    caplog.set_level(logging.INFO)

    _pedir(cabecalho_de(usuario), email_novo)
    token = _token_do_link(provedor.enviadas[0])
    _confirmar(token)

    texto = caplog.text
    assert "troca_email resultado=pedido" in texto
    assert "troca_email resultado=confirmado" in texto
    for proibido in (email_novo, usuario.email, token, "confirmar-email#"):
        assert proibido not in texto
