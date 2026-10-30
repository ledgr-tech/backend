"""Webhook da Stripe (issues #166 e #167, ADR-026): assinatura verificada sobre
o corpo cru, idempotência, cada tipo de evento, a ordem de chegada e o que não
vai para o log. A Stripe é a de tests/stripe_falso.py.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime

import pytest
import stripe
from conftest import SEGREDO_WEBHOOK_TESTE
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from stripe_falso import assinatura_da_stripe

from app.core.config import settings
from app.models import AssinaturaStripe, Conta, EventoStripe, PagamentoStripe
from main import app

client = TestClient(app)

AGORA = int(time.time())
FIM_DO_PERIODO = 1_800_000_000


def _assinar(corpo: bytes, segredo: str = SEGREDO_WEBHOOK_TESTE, instante: int | None = None):
    # A hora de agora, e não a da importação do módulo: a suíte inteira passa
    # dos 5 minutos de tolerância da Stripe.
    instante = int(time.time()) if instante is None else instante
    esperada = hmac.new(segredo.encode(), f"{instante}.".encode() + corpo, hashlib.sha256)
    return f"t={instante},v1={esperada.hexdigest()}"


def _evento(tipo: str, objeto: dict, evento_id: str | None = None, created: int | None = None):
    return {
        "id": evento_id or f"evt_teste_{uuid.uuid4().hex}",
        "object": "event",
        "type": tipo,
        "created": created or AGORA,
        "livemode": False,
        "data": {"object": objeto},
    }


def _enviar(evento: dict, **cabecalhos):
    corpo = json.dumps(evento).encode()
    return client.post(
        "/stripe/webhook",
        content=corpo,
        headers={"Content-Type": "application/json", "Stripe-Signature": _assinar(corpo)}
        | cabecalhos,
    )


@pytest.fixture(autouse=True)
def _limpar_eventos(db_session):
    yield
    db_session.rollback()
    db_session.execute(delete(EventoStripe).where(EventoStripe.id.like("evt_teste_%")))
    db_session.commit()


@pytest.fixture
def conta(db_session, criar_empresa, stripe_falso):
    """Uma conta já ligada a um cliente da Stripe, com o teste em andamento."""
    conta = db_session.get(Conta, criar_empresa().conta_id)
    conta.stripe_customer_id = f"cus_{uuid.uuid4().hex[:14]}"
    conta.teste_termina_em = datetime(2026, 12, 1, tzinfo=UTC)
    db_session.commit()
    return conta


def _assinatura(db_session, conta_id) -> AssinaturaStripe | None:
    db_session.expire_all()
    return db_session.scalar(select(AssinaturaStripe).where(AssinaturaStripe.conta_id == conta_id))


def _evento_gravado(db_session, evento_id: str) -> EventoStripe | None:
    db_session.expire_all()
    return db_session.get(EventoStripe, evento_id)


def _total_de_eventos(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(
        select(func.count()).select_from(EventoStripe).where(EventoStripe.id.like("evt_teste_%"))
    )


def _na_stripe(stripe_falso, conta, subscription_id="sub_1", **opcoes) -> str:
    stripe_falso.assinaturas[subscription_id] = assinatura_da_stripe(
        subscription_id, conta_id=conta.id, cliente_id=conta.stripe_customer_id, **opcoes
    )
    return subscription_id


def _fatura(conta, fatura_id="in_1", **campos) -> dict:
    return {
        "id": fatura_id,
        "object": "invoice",
        "customer": conta.stripe_customer_id,
        "amount_paid": 29900,
        "amount_due": 29900,
        "currency": "brl",
        "period_start": AGORA,
        "period_end": AGORA,
        "lines": {"data": [{"period": {"start": 1_790_000_000, "end": FIM_DO_PERIODO}}]},
        "status_transitions": {"paid_at": 1_790_000_100},
        "parent": {"subscription_details": {"metadata": {"conta_id": str(conta.id)}}},
        **campos,
    }


# --- verificação da assinatura ----------------------------------------------------------


def test_sem_assinatura_da_400_e_nao_grava_nada(db_session, conta):
    evento = _evento("customer.subscription.created", {"id": "sub_1"})

    resposta = client.post("/stripe/webhook", content=json.dumps(evento).encode())

    assert resposta.status_code == 400
    assert _total_de_eventos(db_session) == 0
    assert _assinatura(db_session, conta.id) is None


def test_assinatura_invalida_da_400_e_nao_grava_nada(db_session, conta, stripe_falso):
    _na_stripe(stripe_falso, conta)
    evento = _evento("customer.subscription.created", {"id": "sub_1"})
    corpo = json.dumps(evento).encode()

    resposta = client.post(
        "/stripe/webhook",
        content=corpo,
        headers={"Stripe-Signature": _assinar(corpo, segredo="whsec_de_outro_endpoint")},
    )

    assert resposta.status_code == 400
    assert _total_de_eventos(db_session) == 0
    assert _assinatura(db_session, conta.id) is None
    assert stripe_falso.chamadas == []


def test_assinatura_de_antes_da_tolerancia_da_400(db_session, conta, stripe_falso):
    evento = _evento("customer.subscription.created", {"id": "sub_1"})
    corpo = json.dumps(evento).encode()

    resposta = client.post(
        "/stripe/webhook",
        content=corpo,
        headers={"Stripe-Signature": _assinar(corpo, instante=int(time.time()) - 3600)},
    )

    assert resposta.status_code == 400
    assert _total_de_eventos(db_session) == 0


def test_corpo_alterado_depois_de_assinado_da_400(db_session, conta):
    evento = _evento("customer.subscription.created", {"id": "sub_1"})
    assinatura = _assinar(json.dumps(evento).encode())
    evento["data"]["object"]["id"] = "sub_outro"

    resposta = client.post(
        "/stripe/webhook",
        content=json.dumps(evento).encode(),
        headers={"Stripe-Signature": assinatura},
    )

    assert resposta.status_code == 400
    assert _total_de_eventos(db_session) == 0


def test_corpo_que_nao_e_json_assinado_da_400(db_session, conta):
    corpo = b"isso nao e json"

    resposta = client.post(
        "/stripe/webhook", content=corpo, headers={"Stripe-Signature": _assinar(corpo)}
    )

    assert resposta.status_code == 400


def test_o_corpo_cru_e_o_que_vale_a_serializacao_nao_importa(db_session, conta, stripe_falso):
    """Espaços e ordem de chaves diferentes do `json.dumps` do Python: a assinatura
    é sobre os bytes recebidos."""
    sub = _na_stripe(stripe_falso, conta)
    evento = _evento("customer.subscription.updated", {"id": sub})
    corpo = json.dumps(evento, indent=4, sort_keys=True, ensure_ascii=False).encode()

    resposta = client.post(
        "/stripe/webhook", content=corpo, headers={"Stripe-Signature": _assinar(corpo)}
    )

    assert resposta.status_code == 200


def test_sem_a_cobranca_ligada_da_503(db_session, conta, monkeypatch):
    monkeypatch.setattr(settings, "stripe_habilitado", False)

    resposta = _enviar(_evento("customer.subscription.created", {"id": "sub_1"}))

    assert resposta.status_code == 503
    assert _total_de_eventos(db_session) == 0


@pytest.mark.parametrize("variavel", ["stripe_secret_key", "stripe_webhook_secret"])
def test_sem_chave_ou_sem_segredo_da_503(db_session, conta, monkeypatch, variavel):
    monkeypatch.setattr(settings, variavel, "")

    assert _enviar(_evento("customer.subscription.created", {"id": "sub_1"})).status_code == 503


def test_nao_exige_jwt_nem_empresa_nem_segredo_do_proxy_e_nao_tem_rate_limit(
    db_session, conta, stripe_falso, monkeypatch
):
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", "segredo-do-proxy")
    sub = _na_stripe(stripe_falso, conta)

    respostas = [client.post("/stripe/webhook", content=b"{}") for _ in range(30)]
    valida = _enviar(_evento("customer.subscription.created", {"id": sub}))

    assert {r.status_code for r in respostas} == {400}
    assert valida.status_code == 200
    assert "Authorization" not in valida.request.headers
    assert "X-Ledgr-Empresa" not in valida.request.headers


def test_a_rota_nao_aparece_no_openapi():
    assert not [caminho for caminho in app.openapi()["paths"] if caminho.startswith("/stripe")]


# --- idempotência ---------------------------------------------------------------------------


def test_evento_duplicado_e_processado_uma_vez(db_session, conta, stripe_falso):
    sub = _na_stripe(stripe_falso, conta)
    evento = _evento("customer.subscription.created", {"id": sub})

    primeira = _enviar(evento)
    stripe_falso.assinaturas[sub]["status"] = "past_due"  # mudou na Stripe depois
    segunda = _enviar(evento)

    assert (primeira.status_code, primeira.json()) == (200, {"resultado": "sincronizada"})
    assert (segunda.status_code, segunda.json()) == (200, {"resultado": "duplicado"})
    assert stripe_falso.nomes().count("Subscription.retrieve") == 1
    assert _assinatura(db_session, conta.id).status == "active"
    gravado = _evento_gravado(db_session, evento["id"])
    assert gravado.tipo == "customer.subscription.created"
    assert gravado.recebido_em is not None
    assert gravado.processado_em is not None


def test_eventos_stripe_guarda_so_id_tipo_e_datas(db_session):
    colunas = {coluna.name for coluna in EventoStripe.__table__.columns}

    assert colunas == {"id", "tipo", "recebido_em", "processado_em"}


# --- tipos tratados ----------------------------------------------------------------------------


def test_checkout_concluido_liga_o_cliente_e_a_assinatura_a_conta(
    db_session, criar_empresa, stripe_falso
):
    conta = db_session.get(Conta, criar_empresa().conta_id)
    assert conta.stripe_customer_id is None
    stripe_falso.assinaturas["sub_1"] = assinatura_da_stripe(
        "sub_1",
        conta_id=conta.id,
        cliente_id="cus_novo",
        status="trialing",
        lookup_key="avancado_semestral",
    )
    sessao = {
        "id": "cs_test_1",
        "mode": "subscription",
        "client_reference_id": str(conta.id),
        "customer": "cus_novo",
        "subscription": "sub_1",
        "payment_status": "no_payment_required",
    }

    resposta = _enviar(_evento("checkout.session.completed", sessao))

    assert resposta.status_code == 200
    db_session.expire_all()
    assert db_session.get(Conta, conta.id).stripe_customer_id == "cus_novo"
    assinatura = _assinatura(db_session, conta.id)
    assert (assinatura.stripe_subscription_id, assinatura.status) == ("sub_1", "trialing")
    assert (assinatura.plano, assinatura.periodo) == ("avancado", "semestral")


def test_checkout_de_conta_que_nao_existe_e_ignorado(db_session, stripe_falso):
    sessao = {
        "id": "cs_test_1",
        "mode": "subscription",
        "client_reference_id": str(uuid.uuid4()),
        "customer": "cus_x",
        "subscription": "sub_1",
    }

    resposta = _enviar(_evento("checkout.session.completed", sessao))

    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "sem_conta"}
    assert stripe_falso.chamadas == []


@pytest.mark.parametrize(
    "tipo",
    [
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ],
)
def test_eventos_da_assinatura_sincronizam_o_banco_buscando_na_api(
    db_session, conta, stripe_falso, tipo
):
    status = "canceled" if tipo.endswith("deleted") else "active"
    sub = _na_stripe(
        stripe_falso,
        conta,
        status=status,
        lookup_key="padrao_trimestral",
        cancelar_no_fim=True,
        cancelada_em=1_795_000_000,
    )
    # O payload do evento só serve para achar a assinatura: o estado vem da API.
    evento = _evento(tipo, {"id": sub, "status": "incomplete", "items": {"data": []}})

    resposta = _enviar(evento)

    assert resposta.status_code == 200
    assert stripe_falso.chamadas_de("Subscription.retrieve")[0]["id"] == sub
    assinatura = _assinatura(db_session, conta.id)
    assert assinatura.status == status
    assert (assinatura.plano, assinatura.periodo) == ("padrao", "trimestral")
    assert assinatura.stripe_subscription_id == sub
    assert assinatura.periodo_atual_fim == datetime.fromtimestamp(FIM_DO_PERIODO, UTC)
    assert assinatura.cancelar_no_fim_do_periodo is True
    assert assinatura.cancelada_em == datetime.fromtimestamp(1_795_000_000, UTC)
    assert assinatura.atualizada_em is not None


def test_a_busca_na_api_usa_a_versao_fixada_e_a_chave(db_session, conta, stripe_falso):
    sub = _na_stripe(stripe_falso, conta)

    _enviar(_evento("customer.subscription.created", {"id": sub}))

    chamada = stripe_falso.chamadas_de("Subscription.retrieve")[0]
    assert chamada["stripe_version"] == stripe.api_version
    assert chamada["api_key"] == "sk_test_falsa"


def test_a_conta_tambem_e_achada_pelo_cliente_sem_a_metadata(db_session, conta, stripe_falso):
    sub = _na_stripe(stripe_falso, conta, metadata=False)

    _enviar(_evento("customer.subscription.created", {"id": sub}))

    assert _assinatura(db_session, conta.id).status == "active"


def test_evento_fora_de_ordem_nao_regride_o_status(db_session, conta, stripe_falso):
    """O `deleted` chega primeiro; o `updated` (que na hora dizia `active`) chega
    depois. Como cada um busca a assinatura de novo, o estado final é o da API."""
    sub = _na_stripe(stripe_falso, conta, status="canceled")

    _enviar(_evento("customer.subscription.deleted", {"id": sub}))
    atrasado = _enviar(
        _evento(
            "customer.subscription.updated", {"id": sub, "status": "active"}, created=AGORA - 60
        )
    )

    assert atrasado.status_code == 200
    assert _assinatura(db_session, conta.id).status == "canceled"


def test_evento_atrasado_de_assinatura_antiga_nao_derruba_a_nova(db_session, conta, stripe_falso):
    nova = _na_stripe(stripe_falso, conta, "sub_nova", status="active", lookup_key="avancado_anual")
    antiga = _na_stripe(stripe_falso, conta, "sub_antiga", status="canceled")
    _enviar(_evento("customer.subscription.created", {"id": nova}))

    resposta = _enviar(_evento("customer.subscription.deleted", {"id": antiga}))

    assert resposta.json() == {"resultado": "assinatura_antiga"}
    assinatura = _assinatura(db_session, conta.id)
    assert (assinatura.stripe_subscription_id, assinatura.status) == ("sub_nova", "active")


def test_assinatura_nova_substitui_a_que_ja_terminou(db_session, conta, stripe_falso):
    antiga = _na_stripe(stripe_falso, conta, "sub_antiga", status="canceled")
    _enviar(_evento("customer.subscription.deleted", {"id": antiga}))
    nova = _na_stripe(stripe_falso, conta, "sub_nova", status="active", lookup_key="avancado_anual")

    _enviar(_evento("customer.subscription.created", {"id": nova}))

    db_session.expire_all()
    linhas = db_session.scalars(
        select(AssinaturaStripe).where(AssinaturaStripe.conta_id == conta.id)
    ).all()
    assert [(a.stripe_subscription_id, a.status, a.plano) for a in linhas] == [
        ("sub_nova", "active", "avancado")
    ]


def test_assinatura_que_nao_existe_mais_na_stripe_e_ignorada(db_session, conta, stripe_falso):
    resposta = _enviar(_evento("customer.subscription.updated", {"id": "sub_sumida"}))

    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "nao_existe"}
    assert _assinatura(db_session, conta.id) is None


def test_preco_que_nao_e_do_ledgr_e_ignorado(db_session, conta, stripe_falso):
    sub = _na_stripe(stripe_falso, conta, lookup_key="outro_produto")

    resposta = _enviar(_evento("customer.subscription.created", {"id": sub}))

    assert resposta.json() == {"resultado": "preco_desconhecido"}
    assert _assinatura(db_session, conta.id) is None


def test_assinatura_de_conta_desconhecida_e_ignorada(db_session, stripe_falso):
    stripe_falso.assinaturas["sub_1"] = assinatura_da_stripe(
        "sub_1", conta_id=uuid.uuid4(), cliente_id="cus_que_ninguem_tem"
    )

    resposta = _enviar(_evento("customer.subscription.created", {"id": "sub_1"}))

    assert resposta.json() == {"resultado": "sem_conta"}


# --- faturas -------------------------------------------------------------------------------------


def _pagamento(db_session, fatura_id="in_1") -> PagamentoStripe | None:
    db_session.expire_all()
    return db_session.scalar(
        select(PagamentoStripe).where(PagamentoStripe.stripe_invoice_id == fatura_id)
    )


def test_fatura_paga_grava_o_pagamento(db_session, conta):
    resposta = _enviar(_evento("invoice.paid", _fatura(conta, amount_paid=85500)))

    assert resposta.status_code == 200
    pagamento = _pagamento(db_session)
    assert pagamento.conta_id == conta.id
    assert (pagamento.valor_centavos, pagamento.moeda, pagamento.status) == (85500, "brl", "paga")
    assert pagamento.periodo_inicio == datetime.fromtimestamp(1_790_000_000, UTC)
    assert pagamento.periodo_fim == datetime.fromtimestamp(FIM_DO_PERIODO, UTC)
    assert pagamento.ocorrido_em == datetime.fromtimestamp(1_790_000_100, UTC)


def test_fatura_que_falhou_grava_o_pagamento_com_o_valor_devido(db_session, conta):
    fatura = _fatura(conta, amount_paid=0, amount_due=29900, status_transitions={})

    _enviar(_evento("invoice.payment_failed", fatura, created=AGORA))

    pagamento = _pagamento(db_session)
    assert (pagamento.valor_centavos, pagamento.status) == (29900, "falhou")
    assert pagamento.ocorrido_em == datetime.fromtimestamp(AGORA, UTC)


def test_retentativa_paga_atualiza_a_fatura_que_tinha_falhado(db_session, conta):
    _enviar(_evento("invoice.payment_failed", _fatura(conta, amount_paid=0)))
    _enviar(_evento("invoice.paid", _fatura(conta)))

    pagamento = _pagamento(db_session)
    assert pagamento.status == "paga"
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(PagamentoStripe)
            .where(PagamentoStripe.stripe_invoice_id == "in_1")
        )
        == 1
    )


def test_falha_que_chega_depois_do_pagamento_nao_desfaz_o_pagamento(db_session, conta):
    _enviar(_evento("invoice.paid", _fatura(conta)))

    resposta = _enviar(_evento("invoice.payment_failed", _fatura(conta, amount_paid=0)))

    assert resposta.json() == {"resultado": "falha_atrasada"}
    pagamento = _pagamento(db_session)
    assert (pagamento.status, pagamento.valor_centavos) == ("paga", 29900)


def test_fatura_sem_periodo_nas_linhas_usa_o_da_fatura(db_session, conta):
    _enviar(_evento("invoice.paid", _fatura(conta, lines={"data": []})))

    pagamento = _pagamento(db_session)
    assert pagamento.periodo_inicio == datetime.fromtimestamp(AGORA, UTC)


def test_fatura_de_cliente_desconhecido_e_ignorada(db_session, conta):
    fatura = _fatura(conta, customer="cus_desconhecido", parent=None)

    resposta = _enviar(_evento("invoice.paid", fatura))

    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "sem_conta"}
    assert _pagamento(db_session) is None


def test_fatura_acha_a_conta_pela_metadata_na_versao_antiga_da_api(db_session, conta):
    fatura = _fatura(
        conta,
        customer="cus_outro",
        parent=None,
        subscription_details={"metadata": {"conta_id": str(conta.id)}},
    )

    _enviar(_evento("invoice.paid", fatura))

    assert _pagamento(db_session).conta_id == conta.id


# --- outros tipos e erros ----------------------------------------------------------------------------


def test_tipo_nao_tratado_e_registrado_e_ignorado(db_session, conta, stripe_falso):
    evento = _evento("customer.updated", {"id": conta.stripe_customer_id})

    resposta = _enviar(evento)

    assert (resposta.status_code, resposta.json()) == (200, {"resultado": "ignorado"})
    gravado = _evento_gravado(db_session, evento["id"])
    assert (gravado.tipo, gravado.processado_em is not None) == ("customer.updated", True)
    assert stripe_falso.chamadas == []
    assert _assinatura(db_session, conta.id) is None


def test_erro_inesperado_da_500_e_nao_marca_o_evento_para_a_stripe_reenviar(
    db_session, conta, stripe_falso, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    sub = _na_stripe(stripe_falso, conta)
    stripe_falso.erro = stripe.APIConnectionError("falha de rede com pessoa@exemplo.com")
    evento = _evento("customer.subscription.created", {"id": sub})

    primeira = _enviar(evento)

    assert primeira.status_code == 500
    assert _evento_gravado(db_session, evento["id"]) is None
    assert _assinatura(db_session, conta.id) is None
    assert "stripe_webhook tipo=customer.subscription.created resultado=erro" in caplog.text
    assert "APIConnectionError" in caplog.text
    assert "pessoa@exemplo.com" not in caplog.text

    stripe_falso.erro = None
    reenvio = _enviar(evento)

    assert (reenvio.status_code, reenvio.json()) == (200, {"resultado": "sincronizada"})
    assert _assinatura(db_session, conta.id).status == "active"


def test_o_log_nao_tem_email_nome_cnpj_nem_payload(db_session, conta, stripe_falso, caplog):
    caplog.set_level(logging.DEBUG)
    sub = _na_stripe(stripe_falso, conta)
    sensivel = {
        "customer_email": "pessoa.privada@exemplo.com",
        "customer_name": "Maria da Silva Privada",
        "customer_tax_ids": [{"type": "br_cnpj", "value": "12.345.678/0001-95"}],
    }
    _enviar(_evento("customer.subscription.created", {"id": sub, **sensivel}))
    _enviar(_evento("invoice.paid", _fatura(conta, **sensivel)))
    _enviar(_evento("invoice.payment_failed", _fatura(conta, fatura_id="in_2", **sensivel)))

    registrado = caplog.text
    assert "stripe_webhook tipo=customer.subscription.created resultado=sincronizada" in registrado
    assert "pessoa.privada" not in registrado
    assert "Maria da Silva" not in registrado
    assert "12.345.678" not in registrado
    assert conta.razao_social not in registrado


# --- o que a conta enxerga ----------------------------------------------------------------------------


def _me(gerar_token, usuario):
    token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
    resposta = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def test_so_o_webhook_muda_a_situacao_da_conta(
    db_session, conta, stripe_falso, criar_usuario, gerar_token
):
    administrador = criar_usuario(conta_id=conta.id, papel="admin")
    antes = _me(gerar_token, administrador)
    assert antes["assinatura"] is None
    sub = _na_stripe(
        stripe_falso, conta, status="past_due", lookup_key="avancado_mensal", cancelar_no_fim=True
    )

    _enviar(_evento("customer.subscription.updated", {"id": sub}))

    depois = _me(gerar_token, administrador)
    assert depois["situacao"] == "pagamento_pendente"
    assert depois["assinatura"] == {
        "status": "past_due",
        "plano": "avancado",
        "periodo": "mensal",
        "periodo_atual_fim": datetime.fromtimestamp(FIM_DO_PERIODO, UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "cancelar_no_fim_do_periodo": True,
    }
