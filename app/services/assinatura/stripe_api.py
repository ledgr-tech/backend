"""As chamadas à Stripe, num lugar só (issues #166 e #167, ADR-026).

- A versão da API é a `API_VERSION` abaixo, enviada em toda chamada. É a mesma
  que a biblioteca `stripe` fixada em requirements.in traz (tests/test_stripe_api.py
  confere): subir a biblioteca é revisar esta versão junto.
- A chave vem de `STRIPE_SECRET_KEY`, só quando `STRIPE_HABILITADO` está ligada;
  senão `exigir_stripe` dá 503, e nada é chamado. O backend nunca recebe nem
  guarda dado de cartão: quem coleta o cartão é a página de checkout da Stripe.
- Nenhum preço mora no backend: o preço do plano e do período é achado pela
  `lookup_key` "<plano>_<periodo>", criada por scripts/stripe_catalogo.py.
- Os objetos voltam como `dict` (`to_dict()`): o `StripeObject` da biblioteca
  não é um dict, e `.items` de uma assinatura é a lista de itens, não o método.
- Erro da Stripe (`stripe.StripeError`) sobe como está. As rotas do app o
  viram 502; o webhook, 500, para a Stripe reenviar. Nada é logado com a
  mensagem do erro, só a classe.
"""

import logging
from typing import Any

import stripe
from fastapi import HTTPException
from fastapi import status as http_status

from app.core.config import settings
from app.core.plano import PERIODOS, PLANOS

logger = logging.getLogger(__name__)

API_VERSION = "2026-10-28.endive"
TIMEOUT_SEGUNDOS = 15
TENTATIVAS_DE_REDE = 2

MENSAGEM_INDISPONIVEL = "A cobrança não está disponível no momento."
MENSAGEM_FALHA_NA_STRIPE = "Não foi possível falar com a Stripe agora. Tente de novo em instantes."

# O timeout padrão da biblioteca é de 80 segundos: o webhook e o checkout não
# esperam isso.
stripe.max_network_retries = TENTATIVAS_DE_REDE
stripe.default_http_client = stripe.RequestsClient(timeout=TIMEOUT_SEGUNDOS)


def exigir_stripe() -> str:
    """A chave da Stripe, ou 503 com a flag desligada ou a chave vazia."""
    if not settings.stripe_disponivel():
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=MENSAGEM_INDISPONIVEL
        )
    return settings.stripe_secret_key.strip()


def exigir_segredo_do_webhook() -> str:
    """O segredo do webhook (`whsec_`), ou 503 se a Stripe não está disponível
    ou o segredo não foi configurado: nada seria verificável."""
    exigir_stripe()
    segredo = settings.stripe_webhook_secret.strip()
    if not segredo:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=MENSAGEM_INDISPONIVEL
        )
    return segredo


def _opcoes(chave: str, **extra: Any) -> dict[str, Any]:
    return {"api_key": chave, "stripe_version": API_VERSION, **extra}


def lookup_key(plano: str, periodo: str) -> str:
    return f"{plano}_{periodo}"


def plano_e_periodo(chave: str | None) -> tuple[str, str] | None:
    """O plano e o período de uma `lookup_key` "<plano>_<periodo>", ou `None`
    se a chave não é do Ledgr."""
    plano, separador, periodo = (chave or "").partition("_")
    if separador and plano in PLANOS and periodo in PERIODOS:
        return plano, periodo
    return None


def erro_da_stripe(exc: stripe.StripeError, onde: str) -> HTTPException:
    """O 502 das rotas do app, com só a categoria no log."""
    logger.error("stripe_api resultado=erro onde=%s erro=%s", onde, type(exc).__name__)
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY, detail=MENSAGEM_FALHA_NA_STRIPE
    )


def buscar_preco(chave: str, lookup: str) -> dict | None:
    """O preço ativo da `lookup_key`, ou `None` se o catálogo não o tem."""
    lista = stripe.Price.list(lookup_keys=[lookup], active=True, limit=1, **_opcoes(chave))
    return lista.data[0].to_dict() if lista.data else None


def _cnpj_formatado(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def criar_cliente(chave: str, *, conta_id: str, razao_social: str, cnpj: str, email: str) -> str:
    """Cria o Customer da conta e devolve o id. O CNPJ vai como tax id
    `br_cnpj`; se a Stripe o recusar (formato novo, por exemplo), cria só com a
    metadata."""
    dados = {
        "name": razao_social,
        "email": email,
        "metadata": {"conta_id": conta_id},
    }
    try:
        cliente = stripe.Customer.create(
            **dados,
            tax_id_data=[{"type": "br_cnpj", "value": _cnpj_formatado(cnpj)}],
            **_opcoes(chave, idempotency_key=f"ledgr-cliente-{conta_id}"),
        )
    except stripe.InvalidRequestError:
        logger.info("stripe_api resultado=cliente_sem_cnpj")
        cliente = stripe.Customer.create(
            **dados, **_opcoes(chave, idempotency_key=f"ledgr-cliente-{conta_id}-sem-cnpj")
        )
    return cliente.id


def criar_sessao_de_checkout(
    chave: str,
    *,
    cliente_id: str,
    preco_id: str,
    conta_id: str,
    trial_end: int | None,
    success_url: str,
    cancel_url: str,
    chave_de_idempotencia: str,
) -> str:
    """Cria a Checkout Session em modo assinatura e devolve a URL. O cartão é
    coletado pela Stripe, só cartão de crédito."""
    dados_da_assinatura: dict[str, Any] = {"metadata": {"conta_id": conta_id}}
    if trial_end is not None:
        dados_da_assinatura["trial_end"] = trial_end
    sessao = stripe.checkout.Session.create(
        mode="subscription",
        customer=cliente_id,
        line_items=[{"price": preco_id, "quantity": 1}],
        client_reference_id=conta_id,
        metadata={"conta_id": conta_id},
        subscription_data=dados_da_assinatura,
        allow_promotion_codes=True,
        locale="pt-BR",
        payment_method_types=["card"],
        success_url=success_url,
        cancel_url=cancel_url,
        **_opcoes(chave, idempotency_key=chave_de_idempotencia),
    )
    return sessao.url


def buscar_assinatura(chave: str, subscription_id: str) -> dict:
    return stripe.Subscription.retrieve(subscription_id, **_opcoes(chave)).to_dict()


def evento_do_webhook(corpo: bytes, assinatura: str | None, segredo: str) -> dict:
    """Verifica a assinatura do webhook sobre o corpo CRU e devolve o evento.
    Levanta `ValueError` (corpo que não é JSON de evento) ou
    `stripe.SignatureVerificationError` (assinatura ausente, inválida ou
    antiga); nos dois casos a rota responde 400 sem gravar nada."""
    return stripe.Webhook.construct_event(corpo, assinatura, segredo).to_dict()
