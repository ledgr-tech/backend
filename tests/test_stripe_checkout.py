"""Checkout da assinatura (issues #166 e #167, ADR-026): `POST /assinatura/checkout`
e o `POST /empresa/plano` obsoleto. A Stripe é a de tests/stripe_falso.py.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import stripe
from conftest import FRONTEND_URL_TESTE
from fastapi.testclient import TestClient

from app.core import auth
from app.core.config import settings
from app.core.plano import DURACAO_TESTE
from app.models import AssinaturaStripe, Conta
from main import app

client = TestClient(app)

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
FIM_DO_TESTE = T0 + DURACAO_TESTE


@pytest.fixture(autouse=True)
def _relogio(monkeypatch):
    monkeypatch.setattr(auth, "_agora", lambda: T0)


@pytest.fixture
def cabecalho(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        auth_time = math.floor(auth._agora().timestamp())
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, auth_time=auth_time)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


@pytest.fixture
def admin(db_session, criar_usuario):
    """Administrador de uma conta que acabou de se cadastrar: no teste, sem plano."""
    usuario = criar_usuario()
    db_session.get(Conta, usuario.conta_id).teste_termina_em = FIM_DO_TESTE
    db_session.commit()
    return usuario


@pytest.fixture
def precos(stripe_falso):
    """O catálogo da Stripe com os 12 preços do Ledgr."""
    for plano in ("essencial", "padrao", "avancado"):
        for periodo in ("mensal", "trimestral", "semestral", "anual"):
            chave = f"{plano}_{periodo}"
            stripe_falso.precos[f"price_{chave}"] = {
                "id": f"price_{chave}",
                "object": "price",
                "active": True,
                "lookup_key": chave,
            }
    return stripe_falso


def _checkout(headers, plano="padrao", periodo="mensal", **cabecalhos):
    return client.post(
        "/assinatura/checkout",
        headers={**headers, **cabecalhos},
        json={"plano": plano, "periodo": periodo},
    )


def _conta(db_session, conta_id) -> Conta:
    db_session.expire_all()
    return db_session.get(Conta, conta_id)


def _assinar(db_session, conta_id, status: str) -> None:
    db_session.add(
        AssinaturaStripe(
            conta_id=conta_id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
            status=status,
            plano="essencial",
            periodo="mensal",
        )
    )
    db_session.commit()


# --- recusas ---------------------------------------------------------------------------


def test_sem_a_cobranca_ligada_da_503_e_nao_chama_a_stripe(admin, cabecalho, precos, monkeypatch):
    monkeypatch.setattr(settings, "stripe_habilitado", False)

    resposta = _checkout(cabecalho(admin))

    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "A cobrança não está disponível no momento."}
    assert precos.chamadas == []


def test_sem_a_chave_da_503(admin, cabecalho, precos, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", " ")

    assert _checkout(cabecalho(admin)).status_code == 503
    assert precos.chamadas == []


def test_sem_frontend_url_da_503_em_vez_de_url_de_retorno_quebrada(
    admin, cabecalho, precos, monkeypatch
):
    monkeypatch.setattr(settings, "frontend_url", "")

    assert _checkout(cabecalho(admin)).status_code == 503
    assert precos.chamadas == []


def test_so_o_admin_faz_checkout(db_session, admin, criar_usuario, cabecalho, precos):
    gestor = criar_usuario(conta_id=admin.conta_id)

    resposta = _checkout(cabecalho(gestor))

    assert resposta.status_code == 403
    assert precos.chamadas == []


def test_sem_token_da_401(precos):
    resposta = client.post("/assinatura/checkout", json={"plano": "padrao", "periodo": "mensal"})

    assert resposta.status_code == 401


@pytest.mark.parametrize(
    ("corpo", "campo"),
    [
        ({"plano": "volume", "periodo": "mensal"}, "plano"),
        ({"plano": "Padrao", "periodo": "mensal"}, "plano"),
        ({"periodo": "mensal"}, "plano"),
        ({"plano": "padrao", "periodo": "diario"}, "periodo"),
        ({"plano": "padrao", "periodo": None}, "periodo"),
        ({"plano": "padrao"}, "periodo"),
    ],
)
def test_plano_ou_periodo_invalido_da_422_no_campo(admin, cabecalho, precos, corpo, campo):
    resposta = client.post("/assinatura/checkout", headers=cabecalho(admin), json=corpo)

    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["loc"] == ["body", campo]
    assert precos.chamadas == []


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "incomplete"])
def test_conta_com_assinatura_viva_da_409(db_session, admin, cabecalho, precos, status):
    _assinar(db_session, admin.conta_id, status)

    resposta = _checkout(cabecalho(admin))

    assert resposta.status_code == 409
    assert precos.chamadas == []


@pytest.mark.parametrize("status", ["canceled", "unpaid", "incomplete_expired", "paused"])
def test_conta_com_assinatura_encerrada_pode_assinar_de_novo(
    db_session, admin, cabecalho, precos, status
):
    _assinar(db_session, admin.conta_id, status)

    assert _checkout(cabecalho(admin)).status_code == 200


def test_plano_fora_do_catalogo_da_503(admin, cabecalho, precos):
    del precos.precos["price_padrao_anual"]

    resposta = _checkout(cabecalho(admin), "padrao", "anual")

    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "Esse plano não está disponível no momento."}
    assert precos.chamadas_de("checkout.Session.create") == []


def test_preco_arquivado_nao_vale(admin, cabecalho, precos):
    precos.precos["price_padrao_mensal"]["active"] = False

    assert _checkout(cabecalho(admin)).status_code == 503


def test_erro_da_stripe_da_502_sem_a_mensagem_dela(admin, cabecalho, precos, caplog):
    precos.erro = stripe.APIConnectionError("conexão caiu para pessoa@exemplo.com")

    resposta = _checkout(cabecalho(admin))

    assert resposta.status_code == 502
    assert "pessoa@exemplo.com" not in resposta.text + caplog.text
    assert "erro=APIConnectionError" in caplog.text


# --- a sessão ------------------------------------------------------------------------------


def test_cria_a_sessao_de_assinatura_com_os_parametros_combinados(
    db_session, admin, cabecalho, precos
):
    resposta = _checkout(cabecalho(admin), "avancado", "trimestral")

    assert resposta.status_code == 200
    url = resposta.json()["url"]
    assert url.startswith("https://checkout.stripe.com/")
    (sessao,) = precos.chamadas_de("checkout.Session.create")
    conta_id = str(admin.conta_id)
    assert sessao["mode"] == "subscription"
    assert sessao["line_items"] == [{"price": "price_avancado_trimestral", "quantity": 1}]
    assert sessao["client_reference_id"] == conta_id
    assert sessao["metadata"] == {"conta_id": conta_id}
    assert sessao["subscription_data"]["metadata"] == {"conta_id": conta_id}
    assert sessao["allow_promotion_codes"] is True
    assert sessao["locale"] == "pt-BR"
    assert sessao["payment_method_types"] == ["card"]
    assert sessao["success_url"] == f"{FRONTEND_URL_TESTE}/assinatura?checkout=sucesso"
    assert sessao["cancel_url"] == f"{FRONTEND_URL_TESTE}/assinatura?checkout=cancelado"
    assert sessao["idempotency_key"]
    assert sessao["api_key"] == "sk_test_falsa"
    assert sessao["stripe_version"] == stripe.api_version
    assert sessao["customer"] == _conta(db_session, admin.conta_id).stripe_customer_id


def test_o_preco_e_achado_pela_lookup_key(admin, cabecalho, precos):
    _checkout(cabecalho(admin), "essencial", "semestral")

    (busca,) = precos.chamadas_de("Price.list")
    assert busca["lookup_keys"] == ["essencial_semestral"]
    assert busca["active"] is True


def test_cria_o_cliente_uma_vez_e_reaproveita(db_session, admin, cabecalho, precos):
    _checkout(cabecalho(admin))
    _checkout(cabecalho(admin), "avancado", "anual")

    (cliente,) = precos.chamadas_de("Customer.create")
    conta = _conta(db_session, admin.conta_id)
    assert conta.stripe_customer_id.startswith("cus_")
    assert cliente["name"] == conta.razao_social
    assert cliente["email"] == admin.email
    assert cliente["metadata"] == {"conta_id": str(conta.id)}
    assert cliente["idempotency_key"] == f"ledgr-cliente-{conta.id}"
    cnpj = conta.cnpj
    assert cliente["tax_id_data"] == [
        {
            "type": "br_cnpj",
            "value": f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}",
        }
    ]
    sessoes = precos.chamadas_de("checkout.Session.create")
    assert {sessao["customer"] for sessao in sessoes} == {conta.stripe_customer_id}


def test_se_a_stripe_recusar_o_cnpj_cria_o_cliente_so_com_a_metadata(
    db_session, admin, cabecalho, precos
):
    precos.recusar_tax_id = True

    resposta = _checkout(cabecalho(admin))

    assert resposta.status_code == 200
    tentativa, sem_cnpj = precos.chamadas_de("Customer.create")
    assert "tax_id_data" in tentativa
    assert "tax_id_data" not in sem_cnpj
    assert sem_cnpj["metadata"] == {"conta_id": str(admin.conta_id)}
    assert sem_cnpj["idempotency_key"] != tentativa["idempotency_key"]
    assert _conta(db_session, admin.conta_id).stripe_customer_id is not None


def test_a_mesma_chamada_repetida_na_janela_usa_a_mesma_chave_de_idempotencia(
    admin, cabecalho, precos
):
    _checkout(cabecalho(admin))
    _checkout(cabecalho(admin))

    primeira, segunda = precos.chamadas_de("checkout.Session.create")
    assert primeira["idempotency_key"] == segunda["idempotency_key"]


def test_quem_esta_no_teste_so_e_cobrado_no_fim_dele(admin, cabecalho, precos):
    _checkout(cabecalho(admin))

    (sessao,) = precos.chamadas_de("checkout.Session.create")
    assert sessao["subscription_data"]["trial_end"] == int(FIM_DO_TESTE.timestamp())


def test_com_o_teste_encerrado_nao_ha_trial(db_session, admin, cabecalho, precos, monkeypatch):
    monkeypatch.setattr(auth, "_agora", lambda: FIM_DO_TESTE)

    _checkout(cabecalho(admin))

    (sessao,) = precos.chamadas_de("checkout.Session.create")
    assert "trial_end" not in sessao["subscription_data"]


def test_conta_sem_teste_nao_tem_trial(db_session, admin, cabecalho, precos):
    db_session.get(Conta, admin.conta_id).teste_termina_em = None
    db_session.commit()

    _checkout(cabecalho(admin))

    (sessao,) = precos.chamadas_de("checkout.Session.create")
    assert "trial_end" not in sessao["subscription_data"]


@pytest.mark.parametrize(
    ("falta", "tem_trial"),
    [
        (timedelta(days=3), True),
        (timedelta(hours=48, minutes=2), True),
        (timedelta(hours=48), False),
        (timedelta(hours=47), False),
        (timedelta(minutes=5), False),
    ],
)
def test_trial_end_respeita_a_antecedencia_minima_de_48_horas_da_stripe(
    db_session, admin, cabecalho, precos, falta, tem_trial
):
    """Faltando menos que isso, a Stripe recusaria o `trial_end`: cobra na hora."""
    db_session.get(Conta, admin.conta_id).teste_termina_em = T0 + falta
    db_session.commit()

    assert _checkout(cabecalho(admin)).status_code == 200

    (sessao,) = precos.chamadas_de("checkout.Session.create")
    assert ("trial_end" in sessao["subscription_data"]) is tem_trial


def test_o_checkout_nao_libera_nada_so_o_webhook(db_session, admin, cabecalho, precos):
    _checkout(cabecalho(admin))

    conta = _conta(db_session, admin.conta_id)
    assert conta.assinatura_stripe is None
    assert conta.assinatura_ativa is False
    me = client.get("/me", headers=cabecalho(admin)).json()
    assert (me["situacao"], me["assinatura"]) == ("teste", None)


def test_ignora_o_cabecalho_da_empresa(admin, cabecalho, precos):
    resposta = _checkout(cabecalho(admin), **{"X-Ledgr-Empresa": str(uuid.uuid4())})

    assert resposta.status_code == 200


def test_fica_liberado_para_a_conta_inativa(db_session, admin, cabecalho, precos):
    conta = db_session.get(Conta, admin.conta_id)
    conta.inativada_em, conta.motivo_inativacao = T0, "teste"
    db_session.commit()

    assert _checkout(cabecalho(admin)).status_code == 200
    # E a escolha de plano obsoleta, que escreve, segue bloqueada.
    resposta = client.post("/empresa/plano", headers=cabecalho(admin), json={"plano": "padrao"})
    assert resposta.status_code == 403
    assert resposta.json()["codigo"] == "conta_inativa"


def test_rate_limit_por_conta(admin, cabecalho, precos):
    codigos = [_checkout(cabecalho(admin)).status_code for _ in range(11)]

    assert codigos[:10] == [200] * 10
    assert codigos[10] == 429


def test_logs_so_com_categoria(admin, cabecalho, precos, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app")

    _checkout(cabecalho(admin))

    assert "checkout resultado=criado" in caplog.text
    assert admin.email not in caplog.text
    assert admin.nome not in caplog.text


# --- POST /empresa/plano (obsoleto) ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "incomplete"])
def test_escolha_de_plano_com_assinatura_viva_da_409(db_session, admin, cabecalho, status):
    _assinar(db_session, admin.conta_id, status)

    resposta = client.post("/empresa/plano", headers=cabecalho(admin), json={"plano": "padrao"})

    assert resposta.status_code == 409
    assert "área de assinatura" in resposta.json()["detail"]
    assert _conta(db_session, admin.conta_id).plano is None


def test_escolha_de_plano_com_assinatura_encerrada_grava_a_preferencia(
    db_session, admin, cabecalho
):
    _assinar(db_session, admin.conta_id, "canceled")

    resposta = client.post("/empresa/plano", headers=cabecalho(admin), json={"plano": "padrao"})

    assert resposta.status_code == 200
    assert resposta.json()["situacao"] == "assinatura_encerrada"
    assert _conta(db_session, admin.conta_id).plano == "padrao"


def test_escolha_de_plano_no_teste_nao_mexe_no_fim_do_teste(db_session, admin, cabecalho):
    resposta = client.post("/empresa/plano", headers=cabecalho(admin), json={"plano": "avancado"})

    assert resposta.status_code == 200
    assert resposta.json()["situacao"] == "teste"
    conta = _conta(db_session, admin.conta_id)
    assert (conta.plano, conta.teste_termina_em) == ("avancado", FIM_DO_TESTE)
