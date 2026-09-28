"""Testes de app/services/email/resend_provider.py (issue #66, ADR-012) —
`httpx.MockTransport` no lugar da API real: nenhum teste aqui faz chamada
de rede de verdade nem precisa de banco.
"""

import json
import logging

import httpx
import pytest

from app.services.email.base import MensagemEmail, ProvedorEmailIndisponivel
from app.services.email.mensagens import mensagem_recuperacao_senha, mensagem_senha_alterada
from app.services.email.resend_provider import ProvedorResend

API_KEY = "re_teste_nao_usar_em_producao"
REMETENTE = "Ledgr <nao-responda@conta.ledgr.com.br>"
LINK = "https://app.ledgr.com.br/redefinir-senha#token=segredo-do-link"


def _mensagem(**overrides) -> MensagemEmail:
    campos = {
        "para": "maria@empresa.com.br",
        "assunto": "Assunto",
        "texto": "Corpo em texto com segredo-do-link",
        "html": "<p>Corpo em HTML com segredo-do-link</p>",
        "chave_idempotencia": None,
        **overrides,
    }
    return MensagemEmail(**campos)


def _provedor(handler, **kwargs) -> ProvedorResend:
    parametros = {
        "api_key": API_KEY,
        "remetente": REMETENTE,
        "base_url": "https://api.resend.com/",
        "timeout_segundos": 5.0,
        "cliente_http": httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    }
    return ProvedorResend(**parametros)


def _sucesso(capturadas: list):
    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"})

    return handler


def test_envia_post_para_emails_com_corpo_e_autenticacao():
    capturadas: list[httpx.Request] = []

    _provedor(_sucesso(capturadas)).enviar(_mensagem())

    (request,) = capturadas
    assert request.method == "POST"
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "from": REMETENTE,
        "to": ["maria@empresa.com.br"],
        "subject": "Assunto",
        "text": "Corpo em texto com segredo-do-link",
        "html": "<p>Corpo em HTML com segredo-do-link</p>",
    }
    assert "Idempotency-Key" not in request.headers


def test_responder_para_entra_como_reply_to():
    capturadas: list[httpx.Request] = []

    _provedor(_sucesso(capturadas), responder_para="suporte@ledgr.com.br").enviar(_mensagem())

    assert json.loads(capturadas[0].content)["reply_to"] == "suporte@ledgr.com.br"


def test_chave_de_idempotencia_vai_no_header():
    capturadas: list[httpx.Request] = []

    _provedor(_sucesso(capturadas)).enviar(_mensagem(chave_idempotencia="recuperacao/abc"))

    assert capturadas[0].headers["Idempotency-Key"] == "recuperacao/abc"


def test_status_diferente_de_2xx_levanta_http_com_status():
    provedor = _provedor(lambda request: httpx.Response(422, json={"message": "invalid"}))

    with pytest.raises(ProvedorEmailIndisponivel) as exc:
        provedor.enviar(_mensagem())

    assert exc.value.motivo == "http"
    assert exc.value.status_http == 422


def test_timeout_levanta_timeout():
    def handler(request):
        raise httpx.ReadTimeout("lento", request=request)

    with pytest.raises(ProvedorEmailIndisponivel) as exc:
        _provedor(handler).enviar(_mensagem())

    assert exc.value.motivo == "timeout"


def test_erro_de_rede_levanta_http_sem_status():
    def handler(request):
        raise httpx.ConnectError("sem rede", request=request)

    with pytest.raises(ProvedorEmailIndisponivel) as exc:
        _provedor(handler).enviar(_mensagem())

    assert exc.value.motivo == "http"
    assert exc.value.status_http is None


def test_sem_chave_levanta_nao_configurado_sem_chamar_a_api():
    capturadas: list[httpx.Request] = []

    with pytest.raises(ProvedorEmailIndisponivel) as exc:
        _provedor(_sucesso(capturadas), api_key="").enviar(_mensagem())

    assert exc.value.motivo == "nao_configurado"
    assert capturadas == []


def test_nunca_loga_chave_destinatario_nem_corpo(caplog):
    caplog.set_level(logging.DEBUG)

    _provedor(_sucesso([])).enviar(_mensagem())
    with pytest.raises(ProvedorEmailIndisponivel):
        _provedor(lambda request: httpx.Response(500)).enviar(_mensagem())

    assert "provedor=resend" in caplog.text
    for segredo in (API_KEY, "maria@empresa.com.br", "segredo-do-link"):
        assert segredo not in caplog.text


# mensagens


def test_mensagem_de_recuperacao_leva_o_link_nos_dois_formatos():
    mensagem = mensagem_recuperacao_senha(
        para="maria@empresa.com.br", nome="Maria", link=LINK, validade_minutos=30
    )

    assert mensagem.para == "maria@empresa.com.br"
    assert mensagem.assunto
    assert LINK in mensagem.texto
    assert f'href="{LINK}"' in mensagem.html
    assert "30 minutos" in mensagem.texto


def test_mensagem_de_recuperacao_escapa_o_nome_no_html():
    mensagem = mensagem_recuperacao_senha(
        para="x@y.com", nome="<script>alert(1)</script>", link=LINK, validade_minutos=30
    )

    assert "<script>" not in mensagem.html
    assert "&lt;script&gt;" in mensagem.html


def test_aviso_de_senha_alterada_orienta_quem_nao_reconhece_a_troca():
    mensagem = mensagem_senha_alterada(
        para="maria@empresa.com.br", nome="Maria", link_login="https://app.ledgr.com.br/login"
    )

    assert mensagem.para == "maria@empresa.com.br"
    assert "alterada" in mensagem.assunto.lower()
    assert "Esqueci a senha" in mensagem.texto
    assert "https://app.ledgr.com.br/login" in mensagem.texto
    assert 'href="https://app.ledgr.com.br/login"' in mensagem.html


def test_aviso_de_senha_alterada_sem_link_e_escapa_o_nome():
    mensagem = mensagem_senha_alterada(para="x@y.com", nome="<b>Maria</b>", link_login=None)

    assert "href" not in mensagem.html
    assert "<b>Maria</b>" not in mensagem.html
    assert "&lt;b&gt;Maria&lt;/b&gt;" in mensagem.html
