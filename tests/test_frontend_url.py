"""Validação do FRONTEND_URL como base de link de e-mail (issue #79).

O bug: FRONTEND_URL apontava pra `frontend-hfrnofw9w-ledgr7.vercel.app`, uma URL
de deploy específico da Vercel. Cada deploy do front gera uma URL nova e aposenta
a anterior, então todo link de recuperação de senha já enviado parava de
funcionar. O valor certo é o domínio estável, `https://ledgrfinance.com.br`.

`avaliar_frontend_url` é pura, então estes testes não tocam banco nem rede. O
teste da rota (com banco) está em tests/test_recuperacao_senha.py.
"""

import logging

import pytest

from app.core.config import settings
from app.core.frontend_url import (
    MOTIVO_DEPLOY_VERCEL,
    MOTIVO_ESQUEMA_NAO_HTTPS,
    MOTIVO_QUERY_OU_FRAGMENTO,
    MOTIVO_SEM_ESQUEMA,
    MOTIVO_URL_INVALIDA,
    MOTIVO_VAZIO,
    avaliar_frontend_url,
    avisar_frontend_url_no_startup,
    frontend_url_para_link,
)

# (valor configurado, URL normalizada esperada)
ACEITOS = [
    # O domínio estável de produção: o valor certo desta issue.
    ("https://ledgrfinance.com.br", "https://ledgrfinance.com.br"),
    # A barra no fim sai na normalização, senão o link viria com `//`.
    ("https://ledgrfinance.com.br/", "https://ledgrfinance.com.br"),
    # Alias de produção da Vercel: não tem segmento de deploy, então sobrevive
    # aos deploys e serve.
    (
        "https://frontend-five-azure-50.vercel.app",
        "https://frontend-five-azure-50.vercel.app",
    ),
    # Desenvolvimento: http vale em localhost e 127.0.0.1, com ou sem porta.
    ("http://localhost:3000", "http://localhost:3000"),
    ("http://localhost", "http://localhost"),
    ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
    # Espaço nas pontas é erro de copiar e colar em variável de ambiente.
    ("  https://ledgrfinance.com.br  ", "https://ledgrfinance.com.br"),
    # Caminho serve: o front pode estar sob um prefixo.
    ("https://ledgrfinance.com.br/app", "https://ledgrfinance.com.br/app"),
    ("https://ledgrfinance.com.br/app/", "https://ledgrfinance.com.br/app"),
]

# (valor configurado, motivo esperado)
RECUSADOS = [
    # O valor antigo de produção, que motivou a issue: sem https e deploy
    # específico. A recusa para no esquema, que é a primeira checagem.
    ("frontend-hfrnofw9w-ledgr7.vercel.app", MOTIVO_SEM_ESQUEMA),
    # O mesmo host, agora com https: recusado pelo identificador de deploy
    # (`hfrnofw9w`, 9 caracteres entre o primeiro e o último segmento).
    ("https://frontend-hfrnofw9w-ledgr7.vercel.app", MOTIVO_DEPLOY_VERCEL),
    # Alias de branch da Vercel, que aponta pro último deploy daquela branch.
    ("https://frontend-git-main-ledgr7.vercel.app", MOTIVO_DEPLOY_VERCEL),
    # http fora de localhost: link de e-mail não vai sem TLS.
    ("http://ledgrfinance.com.br", MOTIVO_ESQUEMA_NAO_HTTPS),
    ("", MOTIVO_VAZIO),
    ("   ", MOTIVO_VAZIO),
    # Outros esquemas, e o host local que só vale com http/https.
    ("ftp://ledgrfinance.com.br", MOTIVO_ESQUEMA_NAO_HTTPS),
    ("ledgrfinance.com.br", MOTIVO_SEM_ESQUEMA),
    # Esquema certo, sem host: não dá pra montar link.
    ("https://", MOTIVO_URL_INVALIDA),
    # Query e fragmento: a base é concatenada com `/redefinir-senha#token=...`,
    # então um `#` aqui daria um link com dois fragmentos e uma query ficaria no
    # meio do caminho.
    ("https://ledgrfinance.com.br#x", MOTIVO_QUERY_OU_FRAGMENTO),
    ("https://ledgrfinance.com.br?a=1", MOTIVO_QUERY_OU_FRAGMENTO),
    ("https://ledgrfinance.com.br/?a=1#x", MOTIVO_QUERY_OU_FRAGMENTO),
    # Delimitador sozinho: o parser devolveria query vazia, por isso a checagem
    # olha o caractere cru.
    ("https://ledgrfinance.com.br?", MOTIVO_QUERY_OU_FRAGMENTO),
    ("https://ledgrfinance.com.br#", MOTIVO_QUERY_OU_FRAGMENTO),
]


@pytest.mark.parametrize(("valor", "esperada"), ACEITOS)
def test_aceita_e_normaliza(valor, esperada):
    avaliada = avaliar_frontend_url(valor)

    assert avaliada.serve
    assert avaliada.url == esperada
    assert avaliada.motivo is None
    # A URL normalizada nunca termina em barra: o link é montado concatenando
    # `/redefinir-senha#token=...`.
    assert not avaliada.url.endswith("/")


@pytest.mark.parametrize(("valor", "motivo"), RECUSADOS)
def test_recusa_com_motivo(valor, motivo):
    avaliada = avaliar_frontend_url(valor)

    assert not avaliada.serve
    assert avaliada.url is None
    assert avaliada.motivo == motivo
    assert avaliada.detalhe


def test_alias_de_producao_e_deploy_especifico_se_distinguem_pelo_nome():
    """O par que justifica a regra dos 9 caracteres: os dois são `*.vercel.app`,
    mas só um sobrevive ao deploy seguinte."""
    assert avaliar_frontend_url("https://frontend-five-azure-50.vercel.app").serve
    assert not avaliar_frontend_url("https://frontend-hfrnofw9w-ledgr7.vercel.app").serve


def test_dominio_proprio_com_segmento_de_9_caracteres_nao_e_recusado():
    """A regra do identificador de deploy vale só pra *.vercel.app: fora dela,
    um segmento de 9 caracteres é só um nome."""
    assert avaliar_frontend_url("https://app-hfrnofw9w-x.com.br").serve


@pytest.mark.parametrize("valor", ["https://a-abcdefghi.vercel.app", "https://x.vercel.app"])
def test_vercel_sem_segmento_do_meio_e_aceito(valor):
    """Sem segmento entre o primeiro e o último, não há identificador de deploy
    pra achar — é o alias do projeto."""
    assert avaliar_frontend_url(valor).serve


def test_caminho_sobrevive_mas_query_e_fragmento_nao():
    """O par que explica a regra: o caminho entra no link sem estragar nada, o
    fragmento produziria `...#x/redefinir-senha#token=...`."""
    base = avaliar_frontend_url("https://ledgrfinance.com.br/app").url
    assert f"{base}/redefinir-senha#token=abc".count("#") == 1

    assert not avaliar_frontend_url("https://ledgrfinance.com.br/app#x").serve


# frontend_url_para_link: lê a configuração e loga


def test_para_link_devolve_a_url_normalizada(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://ledgrfinance.com.br/")

    assert frontend_url_para_link() == "https://ledgrfinance.com.br"


def test_para_link_devolve_none_e_loga_error_com_o_motivo(monkeypatch, caplog):
    monkeypatch.setattr(settings, "frontend_url", "https://frontend-hfrnofw9w-ledgr7.vercel.app")

    with caplog.at_level(logging.ERROR):
        assert frontend_url_para_link() is None

    assert len(caplog.records) == 1
    registro = caplog.records[0]
    assert registro.levelno == logging.ERROR
    assert MOTIVO_DEPLOY_VERCEL in registro.getMessage()


def test_para_link_com_url_vazia_nao_loga_error(monkeypatch, caplog):
    """Vazio é o estado documentado de "e-mail desligado" (default de
    `frontend_url`), não configuração errada — ERROR ali seria ruído a cada
    requisição."""
    monkeypatch.setattr(settings, "frontend_url", "")

    with caplog.at_level(logging.ERROR):
        assert frontend_url_para_link() is None

    assert caplog.records == []


def test_log_de_recusa_nao_leva_token_nem_email(monkeypatch, caplog):
    monkeypatch.setattr(settings, "frontend_url", "http://ledgrfinance.com.br")

    with caplog.at_level(logging.ERROR):
        frontend_url_para_link()

    mensagem = caplog.records[0].getMessage()
    assert "token" not in mensagem.lower()
    assert "@" not in mensagem


# avisar_frontend_url_no_startup


def test_startup_loga_error_quando_a_url_configurada_e_recusada(monkeypatch, caplog):
    monkeypatch.setattr(settings, "frontend_url", "https://frontend-git-main-ledgr7.vercel.app")

    with caplog.at_level(logging.ERROR):
        avisar_frontend_url_no_startup()

    assert len(caplog.records) == 1
    assert "origem=startup" in caplog.records[0].getMessage()


@pytest.mark.parametrize("valor", ["https://ledgrfinance.com.br", ""])
def test_startup_silencioso_com_url_boa_ou_vazia(monkeypatch, caplog, valor):
    monkeypatch.setattr(settings, "frontend_url", valor)

    with caplog.at_level(logging.ERROR):
        avisar_frontend_url_no_startup()

    assert caplog.records == []
