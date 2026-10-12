"""E-mail de boas-vindas no cadastro (issue #78).

O `POST /register` agenda o e-mail em `BackgroundTasks` depois do commit; o
`TestClient` roda as tarefas antes de devolver a resposta, então o envio já
aconteceu quando o teste confere. O provedor é o falso do conftest, ou o
`ProvedorResend` de verdade com `httpx.MockTransport` quando o teste precisa do
caminho HTTP. O contrato do `/register` (status, corpo, 409, 422) não muda.
"""

import json
import logging
import re
import uuid

import httpx
import pytest
from conftest import FRONTEND_URL_TESTE
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models import Configuracao, Empresa, Usuario
from app.services.email import ProvedorEmailIndisponivel, ProvedorResend
from app.services.email.envio import obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_boas_vindas
from main import app

client = TestClient(app)

NOME = "Ana Souza"
RAZAO_SOCIAL = "Telha Certa Ltda"
ASSUNTO = "Boas-vindas à Ledgr"


@pytest.fixture
def cadastro(db_session, gerar_cnpj):
    """Payloads de cadastro únicos; no fim, apaga o que eles criaram."""
    emails: list[str] = []
    cnpjs: list[str] = []

    def _payload(**sobrescritas) -> dict:
        dados = {
            "nome": NOME,
            "email": f"{uuid.uuid4().hex[:12]}@teste.com",
            "senha": "senha-forte-123",
            "razao_social": RAZAO_SOCIAL,
            "cnpj": gerar_cnpj(),
            **sobrescritas,
        }
        emails.append(dados["email"].strip().lower())
        cnpjs.append(dados["cnpj"])
        return dados

    yield _payload

    db_session.rollback()
    empresa_ids = [e.id for e in db_session.query(Empresa.id).filter(Empresa.cnpj.in_(cnpjs))]
    db_session.query(Usuario).filter(Usuario.email.in_(emails)).delete(synchronize_session=False)
    db_session.query(Configuracao).filter(Configuracao.empresa_id.in_(empresa_ids)).delete(
        synchronize_session=False
    )
    db_session.query(Empresa).filter(Empresa.id.in_(empresa_ids)).delete(synchronize_session=False)
    db_session.commit()


def _registrar(dados: dict):
    return client.post("/register", json=dados)


@pytest.fixture
def resend_falso(monkeypatch):
    """`ProvedorResend` de verdade, com a API trocada por `httpx.MockTransport`.
    `responder` decide o status; `pedidos` guarda o que chegou à "API"."""
    pedidos: list[httpx.Request] = []
    estado = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return httpx.Response(estado["status"], json={"id": "49a3999c"})

    provedor = ProvedorResend(
        api_key="re_teste",
        remetente="Ledgr <nao-responda@ledgr.test>",
        base_url="https://api.resend.com/",
        timeout_segundos=5.0,
        cliente_http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: provedor
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)
    yield pedidos, estado
    app.dependency_overrides.pop(obter_provedor_email_dependencia, None)


# --- envio -----------------------------------------------------------------------


def test_cadastro_enfileira_uma_mensagem_de_boas_vindas(db_session, provedor, cadastro):
    dados = cadastro(email=f"  Ana.{uuid.uuid4().hex[:8]}@Teste.COM ")

    response = _registrar(dados)

    assert response.status_code == 201
    assert len(provedor.enviadas) == 1
    mensagem = provedor.enviadas[0]
    assert mensagem.para == dados["email"].strip().lower() == response.json()["email"]
    assert mensagem.assunto == ASSUNTO
    assert f"Olá, {NOME}." in mensagem.texto
    assert f"para a empresa {RAZAO_SOCIAL}." in mensagem.texto
    assert f"na tela de login ({FRONTEND_URL_TESTE}/login)" in mensagem.texto
    assert f'<a href="{FRONTEND_URL_TESTE}/login">tela de login</a>' in mensagem.html


def test_chave_de_idempotencia_e_boas_vindas_id_do_usuario(db_session, provedor, cadastro):
    response = _registrar(cadastro())

    assert provedor.enviadas[0].chave_idempotencia == f"boas-vindas-{response.json()['id']}"


def test_pelo_resend_a_chave_vai_no_cabecalho_e_o_texto_no_corpo(
    db_session, resend_falso, cadastro
):
    pedidos, _ = resend_falso

    response = _registrar(cadastro())

    assert response.status_code == 201
    assert len(pedidos) == 1
    assert pedidos[0].headers["Idempotency-Key"] == f"boas-vindas-{response.json()['id']}"
    corpo = json.loads(pedidos[0].content)
    assert corpo["subject"] == ASSUNTO
    assert corpo["to"] == [response.json()["email"]]


def test_com_email_desligado_o_cadastro_responde_igual_e_nada_e_enviado(
    db_session, cadastro, caplog
):
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: None
    dados = cadastro()
    try:
        with caplog.at_level(logging.INFO, logger="app"):
            response = _registrar(dados)
    finally:
        app.dependency_overrides.pop(obter_provedor_email_dependencia, None)

    assert response.status_code == 201
    corpo = response.json()
    assert set(corpo) == {"id", "empresa_id", "nome", "email"}
    assert (corpo["nome"], corpo["email"]) == (NOME, dados["email"])
    assert "boas_vindas resultado=desabilitado" in caplog.text
    assert "resultado=enviado" not in caplog.text


def test_cadastro_com_409_nao_envia(db_session, provedor, cadastro):
    primeiro = cadastro()
    _registrar(primeiro)
    provedor.enviadas.clear()

    email_repetido = _registrar(cadastro(email=primeiro["email"]))
    cnpj_repetido = _registrar(cadastro(cnpj=primeiro["cnpj"]))

    assert (email_repetido.status_code, cnpj_repetido.status_code) == (409, 409)
    assert provedor.enviadas == []


@pytest.mark.parametrize("sobrescrita", [{"email": "nao-e-email"}, {"cnpj": "123"}])
def test_cadastro_com_422_nao_envia(db_session, provedor, cadastro, sobrescrita):
    response = _registrar(cadastro(**sobrescrita))

    assert response.status_code == 422
    assert provedor.enviadas == []


def test_falha_do_provedor_nao_muda_o_201(db_session, provedor, cadastro, caplog):
    provedor._falha = ProvedorEmailIndisponivel("timeout")

    with caplog.at_level(logging.INFO, logger="app"):
        response = _registrar(cadastro())

    assert response.status_code == 201
    assert "boas_vindas resultado=falha_envio motivo=timeout" in caplog.text


def test_erro_http_do_resend_nao_muda_o_201(db_session, resend_falso, cadastro, caplog):
    _, estado = resend_falso
    estado["status"] = 500

    with caplog.at_level(logging.INFO, logger="app"):
        response = _registrar(cadastro())

    assert response.status_code == 201
    assert "boas_vindas resultado=falha_envio motivo=http status_http=500" in caplog.text


def test_erro_inesperado_no_envio_nao_muda_o_201(db_session, provedor, cadastro, caplog):
    provedor._falha = RuntimeError("qualquer coisa")

    with caplog.at_level(logging.INFO, logger="app"):
        response = _registrar(cadastro())

    assert response.status_code == 201
    assert "boas_vindas resultado=falha_envio classe=RuntimeError" in caplog.text


# --- conteúdo --------------------------------------------------------------------


def test_nome_e_razao_social_com_html_saem_escapados(db_session, provedor, cadastro):
    _registrar(cadastro(nome="<b>Ana</b>", razao_social='<i>Telha</i> & "Cia"'))

    html = provedor.enviadas[0].html
    assert "<b>Ana</b>" not in html
    assert "<i>Telha</i>" not in html
    assert "&lt;b&gt;Ana&lt;/b&gt;" in html
    assert "&lt;i&gt;Telha&lt;/i&gt; &amp; &quot;Cia&quot;" in html
    # o texto puro não é HTML: vai como a pessoa escreveu
    assert "Olá, <b>Ana</b>." in provedor.enviadas[0].texto


def test_com_frontend_url_recusada_a_mensagem_sai_sem_link(
    db_session, provedor, cadastro, monkeypatch
):
    monkeypatch.setattr(settings, "frontend_url", "https://frontend-hfrnofw9w-ledgr7.vercel.app")

    response = _registrar(cadastro())

    assert response.status_code == 201
    mensagem = provedor.enviadas[0]
    assert "entre na tela de login e envie" in mensagem.texto
    assert "http" not in mensagem.texto
    assert "<a " not in mensagem.html


def test_texto_puro_completo():
    mensagem = mensagem_boas_vindas(
        para="ana@teste.com",
        nome="Ana",
        razao_social="Telha Certa Ltda",
        link_login="https://ledgrfinance.com.br/login",
        chave_idempotencia="boas-vindas-1",
    )

    assert mensagem.texto == (
        "Olá, Ana.\n\n"
        "Sua conta na Ledgr foi criada para a empresa Telha Certa Ltda.\n\n"
        "Para começar, entre na tela de login (https://ledgrfinance.com.br/login) e envie "
        "o extrato do banco e o extrato do seu sistema de gestão do mesmo período. A Ledgr "
        "cruza os dois e mostra o que bate e o que precisa da sua atenção.\n\n"
        "Se não foi você quem criou esta conta, ignore este e-mail e avise o nosso "
        "suporte.\n\n"
        "Equipe Ledgr"
    )
    assert not re.search(r"<img|<script|https?://(?!ledgrfinance)", mensagem.html)


# --- logs ------------------------------------------------------------------------


def test_nenhum_log_tem_email_nome_ou_razao_social(db_session, provedor, cadastro, caplog):
    dados = cadastro(nome="Beatriz Quixabeira", razao_social="Quixabeira Telhas Ltda")
    provedor._falha = ProvedorEmailIndisponivel("http", status_http=422)

    with caplog.at_level(logging.DEBUG, logger="app"):
        _registrar(dados)
        provedor._falha = None
        _registrar(cadastro(nome="Beatriz Quixabeira", razao_social="Quixabeira Telhas Ltda"))

    assert "boas_vindas resultado=falha_envio" in caplog.text
    assert "boas_vindas resultado=enviado" in caplog.text
    assert dados["email"].split("@")[0] not in caplog.text
    assert "Quixabeira" not in caplog.text
    assert "Beatriz" not in caplog.text
