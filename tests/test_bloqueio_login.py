"""Bloqueio de login por conta (issue #67).

5 senhas erradas para o mesmo e-mail em 15 minutos bloqueiam esse e-mail por 15
minutos: o `POST /login` responde 429 com `Retry-After`, sem chamar o bcrypt. Ver
app/core/bloqueio_login.py.

Cada chamada sai de um IP diferente (`X-Forwarded-For`), para o rate limit por
IP (10/minuto) não interferir: o que se testa aqui é o contador por e-mail. O
estado do bloqueio é zerado entre testes pela fixture `_bloqueio_de_login_zerado`
do conftest.
"""

import logging
import random
import re
import uuid
from datetime import timedelta

import pytest
from conftest import SENHA_USUARIO_TESTE as SENHA
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.core import bloqueio_login, senha
from main import app

client = TestClient(app)

SENHA_ERRADA = "senha-errada-123"
DETALHE_BLOQUEIO = "Muitas tentativas. Tente novamente mais tarde."
INICIO = "2026-10-09 12:00:00"


def _login(email: str, senha_digitada: str = SENHA_ERRADA):
    ip = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    return client.post(
        "/login",
        json={"email": email, "senha": senha_digitada},
        headers={"X-Forwarded-For": ip},
    )


def _errar(email: str, vezes: int) -> list[int]:
    return [_login(email).status_code for _ in range(vezes)]


def _email_sem_conta() -> str:
    return f"{uuid.uuid4().hex[:12]}@teste.com"


def test_cinco_falhas_bloqueiam_e_a_sexta_e_429_mesmo_com_a_senha_certa(criar_usuario):
    usuario = criar_usuario()

    with freeze_time(INICIO):
        assert _errar(usuario.email, 5) == [401] * 5
        resposta = _login(usuario.email, SENHA)

    assert resposta.status_code == 429
    assert resposta.json() == {"detail": DETALHE_BLOQUEIO}
    assert resposta.headers["Retry-After"] == str(15 * 60)


def test_retry_after_conta_o_tempo_que_falta(criar_usuario):
    usuario = criar_usuario()

    with freeze_time(INICIO) as relogio:
        _errar(usuario.email, 5)
        relogio.tick(timedelta(minutes=10, seconds=30))
        resposta = _login(usuario.email, SENHA)

    assert resposta.status_code == 429
    assert resposta.headers["Retry-After"] == str(4 * 60 + 30)


def test_depois_de_15_minutos_o_login_volta_a_funcionar(criar_usuario):
    usuario = criar_usuario()

    with freeze_time(INICIO) as relogio:
        _errar(usuario.email, 5)
        relogio.tick(timedelta(minutes=14, seconds=59))
        assert _login(usuario.email, SENHA).status_code == 429
        relogio.tick(timedelta(seconds=1))
        assert _login(usuario.email, SENHA).status_code == 200


def test_falhas_fora_da_janela_de_15_minutos_nao_se_somam(criar_usuario):
    usuario = criar_usuario()

    with freeze_time(INICIO) as relogio:
        _errar(usuario.email, 4)
        relogio.tick(timedelta(minutes=15, seconds=1))
        assert _errar(usuario.email, 4) == [401] * 4
        assert _login(usuario.email, SENHA).status_code == 200


def test_email_com_e_sem_conta_tem_exatamente_o_mesmo_comportamento(criar_usuario):
    com_conta = criar_usuario().email
    sem_conta = _email_sem_conta()

    with freeze_time(INICIO):
        respostas = {email: [_login(email) for _ in range(6)] for email in (com_conta, sem_conta)}

    def _resumo(resposta):
        cabecalhos = {
            nome: valor for nome, valor in resposta.headers.items() if nome != "content-length"
        }
        return resposta.status_code, resposta.json(), cabecalhos

    assert [_resumo(r) for r in respostas[com_conta]] == [_resumo(r) for r in respostas[sem_conta]]
    assert [r.status_code for r in respostas[sem_conta]] == [401] * 5 + [429]


def test_login_com_sucesso_zera_o_contador(criar_usuario):
    usuario = criar_usuario()

    assert _errar(usuario.email, 4) == [401] * 4
    assert _login(usuario.email, SENHA).status_code == 200
    assert _errar(usuario.email, 4) == [401] * 4
    assert _login(usuario.email, SENHA).status_code == 200


def test_falhas_de_emails_diferentes_nao_se_somam(criar_usuario):
    usuario = criar_usuario()

    for _ in range(3):
        _errar(_email_sem_conta(), 4)
    _errar(usuario.email, 4)

    assert _login(usuario.email, SENHA).status_code == 200


def test_maiusculas_e_espacos_contam_como_o_mesmo_email(criar_usuario):
    usuario = criar_usuario()
    variacoes = [
        usuario.email,
        usuario.email.upper(),
        f"  {usuario.email} ",
        usuario.email.title(),
        f"\t{usuario.email.upper()}",
    ]

    assert [_login(email).status_code for email in variacoes] == [401] * 5
    assert _login(usuario.email, SENHA).status_code == 429


def test_redefinicao_de_senha_com_sucesso_destrava(criar_usuario, provedor):
    usuario = criar_usuario()
    _errar(usuario.email, 5)
    assert _login(usuario.email, SENHA).status_code == 429

    client.post("/senha/recuperar", json={"email": usuario.email})
    link = re.search(r"https://\S+", provedor.enviadas[-1].texto).group(0)
    token = link.split("#token=", 1)[1]
    redefinicao = client.post(
        "/senha/redefinir", json={"token": token, "senha_nova": "senha-nova-456"}
    )

    assert redefinicao.status_code == 204
    assert _login(usuario.email, "senha-nova-456").status_code == 200


def test_durante_o_bloqueio_o_bcrypt_nao_e_chamado(monkeypatch):
    email = _email_sem_conta()
    _errar(email, 5)
    chamadas = []
    monkeypatch.setattr(senha, "verificar", lambda *args: chamadas.append(args) or True)

    assert _login(email).status_code == 429
    assert chamadas == []


def test_log_de_bloqueio_nao_identifica_o_email(criar_usuario, caplog):
    usuario = criar_usuario()

    with caplog.at_level(logging.INFO, logger="app"):
        _errar(usuario.email, 5)
        _login(usuario.email, SENHA)

    mensagens = [registro.getMessage() for registro in caplog.records]
    assert "login_bloqueado motivo=limite_de_falhas" in mensagens
    assert "login_bloqueado motivo=tentativa_durante_bloqueio" in mensagens
    chave = bloqueio_login._chave(usuario.email)
    for mensagem in mensagens:
        assert usuario.email.split("@")[0] not in mensagem
        assert chave not in mensagem
        assert SENHA not in mensagem


# --- o módulo, sem a rota ---------------------------------------------------------


def test_a_chave_e_o_sha256_do_email_normalizado_e_nao_o_email():
    bloqueio_login.registrar_falha("  Maria@Teste.com ")

    chaves = list(bloqueio_login._entradas)
    assert chaves == [bloqueio_login._chave("maria@teste.com")]
    assert len(chaves[0]) == 64
    assert "maria" not in chaves[0]


def test_teto_de_10_000_chaves_descarta_as_mais_antigas():
    emails = [f"pessoa{n}@teste.com" for n in range(bloqueio_login.TETO_CHAVES + 1)]
    for email in emails:
        bloqueio_login.registrar_falha(email)

    assert len(bloqueio_login._entradas) == bloqueio_login.TETO_CHAVES == 10_000
    assert bloqueio_login._chave(emails[0]) not in bloqueio_login._entradas
    assert bloqueio_login._chave(emails[-1]) in bloqueio_login._entradas


def test_no_teto_as_entradas_vencidas_saem_antes_das_mais_antigas(monkeypatch):
    monkeypatch.setattr(bloqueio_login, "TETO_CHAVES", 3)

    with freeze_time(INICIO) as relogio:
        bloqueio_login.registrar_falha("vencida@teste.com")
        relogio.tick(timedelta(minutes=1))
        for _ in range(bloqueio_login.MAXIMO_FALHAS):
            bloqueio_login.registrar_falha("bloqueada@teste.com")
        relogio.tick(timedelta(minutes=14, seconds=30))
        bloqueio_login.registrar_falha("recente@teste.com")
        bloqueio_login.registrar_falha("nova@teste.com")

        # a vencida saiu; a bloqueada, mais antiga que a recente, ficou
        assert bloqueio_login._chave("vencida@teste.com") not in bloqueio_login._entradas
        assert bloqueio_login.bloqueado_ate("bloqueada@teste.com") is not None
        assert len(bloqueio_login._entradas) == 3


@pytest.mark.parametrize("falhas", [1, 4])
def test_menos_de_cinco_falhas_nao_bloqueiam(falhas):
    assert not any(bloqueio_login.registrar_falha("x@teste.com") for _ in range(falhas))
    assert bloqueio_login.bloqueado_ate("x@teste.com") is None


def test_limpar_tira_o_bloqueio():
    for _ in range(bloqueio_login.MAXIMO_FALHAS):
        bloqueio_login.registrar_falha("x@teste.com")
    assert bloqueio_login.bloqueado_ate("X@teste.com ") is not None

    bloqueio_login.limpar(" x@TESTE.com")

    assert bloqueio_login.bloqueado_ate("x@teste.com") is None
