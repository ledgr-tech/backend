"""Molde visual dos e-mails transacionais (issue #188).

Todas as mensagens passam por `_pagina`. O que importa conferir aqui é o que o
visual novo não pode quebrar: o texto puro e o assunto não mudam, nada de
script, e o logo e o mascote só aparecem quando `FRONTEND_URL` serve (a mesma
regra que já vale para os links dos e-mails).
"""

import re

import pytest
from conftest import FRONTEND_URL_TESTE

from app.core.config import settings
from app.services.email import mensagens as m

LINK = "https://app.ledgr.com.br/redefinir-senha#token=abc"

MENSAGENS = {
    "recuperacao_senha": lambda: m.mensagem_recuperacao_senha(
        para="a@x.com", nome="Ana", link=LINK, validade_minutos=60
    ),
    "senha_alterada": lambda: m.mensagem_senha_alterada(
        para="a@x.com", nome="Ana", link_login=f"{FRONTEND_URL_TESTE}/login"
    ),
    "boas_vindas": lambda: m.mensagem_boas_vindas(
        para="a@x.com",
        nome="Ana",
        razao_social="Telha Certa Ltda",
        link_login=f"{FRONTEND_URL_TESTE}/login",
    ),
    "convite": lambda: m.mensagem_convite(
        para="a@x.com",
        nome_de_quem_convidou="Ana",
        razao_social="Telha Certa Ltda",
        link=LINK,
        validade_dias=7,
    ),
    "conta_desativada": lambda: m.mensagem_conta_desativada(
        para="a@x.com", nome="Ana", razao_social_sem_acesso="Telha Certa Ltda"
    ),
    "confirmacao_troca_email": lambda: m.mensagem_confirmacao_troca_email(
        para="a@x.com", nome="Ana", link=LINK, validade_minutos=60
    ),
    "troca_email_pedida": lambda: m.mensagem_troca_email_pedida(
        para="a@x.com", nome="Ana", email_novo="novo@x.com"
    ),
    "troca_email_concluida": lambda: m.mensagem_troca_email_concluida(
        para="a@x.com", nome="Ana", email_novo="novo@x.com"
    ),
    "cupom": lambda: m.mensagem_cupom(
        para="a@x.com",
        nome="Ana",
        razao_social="Telha Certa Ltda",
        desconto="20% por 3 meses",
        codigo="AURORA-7K2M",
        validade="30/11/2026",
        link_assinatura=f"{FRONTEND_URL_TESTE}/assinatura",
    ),
}


@pytest.fixture
def frontend_valida(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)


@pytest.mark.parametrize("nome", MENSAGENS)
def test_com_frontend_url_valida_o_email_leva_o_logo_e_um_mascote(nome, frontend_valida):
    html = MENSAGENS[nome]().html

    imagens = re.findall(r'<img src="([^"]+)"', html)
    assert len(imagens) == 2
    assert all(i.startswith(f"{FRONTEND_URL_TESTE}/email/") and i.endswith(".png") for i in imagens)
    assert imagens[0] == f"{FRONTEND_URL_TESTE}/email/logo.png"
    assert "<h1" in html
    assert "<script" not in html


@pytest.mark.parametrize("nome", MENSAGENS)
def test_sem_frontend_url_que_sirva_o_email_sai_sem_imagem(nome, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "")

    html = MENSAGENS[nome]().html

    assert "<img" not in html
    assert "<h1" in html


def test_dado_do_usuario_continua_escapado_no_molde(frontend_valida):
    mensagem = m.mensagem_convite(
        para="a@x.com",
        nome_de_quem_convidou="<b>Ana</b>",
        razao_social='<i>Telha</i> & "Cia"',
        link=LINK,
        validade_dias=7,
    )

    assert "<b>Ana</b>" not in mensagem.html
    assert "&lt;b&gt;Ana&lt;/b&gt;" in mensagem.html
    assert "&lt;i&gt;Telha&lt;/i&gt; &amp; &quot;Cia&quot;" in mensagem.html
