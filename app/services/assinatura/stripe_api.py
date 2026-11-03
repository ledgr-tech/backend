"""As chamadas à Stripe, num lugar só (issues #166 e #167, ADR-026).

- A versão da API é a `API_VERSION` abaixo, enviada em toda chamada. É a mesma
  que a biblioteca `stripe` fixada em requirements.in traz (tests/test_stripe_api.py
  confere): subir a biblioteca é revisar esta versão junto.
- As formas de pagamento do checkout não vão na requisição: são as do painel da
  Stripe (só cartão, ADR-026). Ver `criar_sessao_de_checkout`.
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
import secrets
from datetime import datetime
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
# Limite da Stripe: o `Price.list` aceita até 10 `lookup_keys` por chamada.
LOOKUP_KEYS_POR_CHAMADA = 10

# Vão em `cancellation_details.comment` e dizem quem cancelou (issue #169): é
# como a sincronização reconhece a origem, já que o `reason` da Stripe não
# distingue o cliente da equipe. Fixas, de propósito: o motivo escrito pelo
# operador fica só no Ledgr, nunca na Stripe.
MARCA_DO_OPERADOR = "ledgr:operador"
# O cancelamento pelo próprio cliente é o PR 2 da #166 (ainda não existe): quem
# o implementar DEVE enviar esta marca em `cancellation_details.comment`; sem
# ela, a origem sai "painel_stripe".
MARCA_DO_CLIENTE = "ledgr:cliente"
# Sem 0, O, 1, I e L: o código é ditado e digitado à mão.
ALFABETO_DO_CODIGO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TAMANHO_DO_CODIGO = 8
CUPONS_POR_CHAMADA = 100

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


def listar_precos(chave: str, lookups: list[str]) -> list[dict]:
    """Os preços ativos das `lookup_keys`, com o produto expandido (uma chamada
    por até 10 chaves, e não uma por produto). A chave que o catálogo não tem
    simplesmente não volta."""
    precos: list[dict] = []
    for inicio in range(0, len(lookups), LOOKUP_KEYS_POR_CHAMADA):
        lista = stripe.Price.list(
            lookup_keys=lookups[inicio : inicio + LOOKUP_KEYS_POR_CHAMADA],
            active=True,
            limit=100,
            expand=["data.product"],
            **_opcoes(chave),
        )
        precos.extend(preco.to_dict() for preco in lista.data)
    return precos


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
    coletado pela Stripe. Não envia `payment_method_types`: na API fixada
    (2026-10-28.endive) o parâmetro não é mais aceito, e as formas de pagamento
    vêm das configurações do painel da Stripe. "Só cartão" é lá que se garante
    (Configurações > Pagamentos > Formas de pagamento), em teste e em live."""
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


# --- ações do operador (issue #169) ---------------------------------------------------


def cancelar_assinatura_no_fim_do_periodo(chave: str, subscription_id: str) -> None:
    """`cancel_at_period_end`: a conta segue com acesso até o fim do período já
    pago. A origem "operador" vai no `cancellation_details.comment`."""
    stripe.Subscription.modify(
        subscription_id,
        cancel_at_period_end=True,
        cancellation_details={"comment": MARCA_DO_OPERADOR},
        **_opcoes(chave),
    )


def cancelar_assinatura_agora(chave: str, subscription_id: str) -> None:
    """Cancela na hora, sem fatura final e sem crédito proporcional: reembolso
    é decisão do painel da Stripe, nunca desta rota."""
    stripe.Subscription.cancel(
        subscription_id,
        cancellation_details={"comment": MARCA_DO_OPERADOR},
        invoice_now=False,
        prorate=False,
        **_opcoes(chave),
    )


def manter_assinatura(chave: str, subscription_id: str) -> None:
    """Desfaz o cancelamento agendado. Limpa o comentário da origem, para um
    cancelamento futuro, de outra origem, não herdar a marca do operador."""
    stripe.Subscription.modify(
        subscription_id,
        cancel_at_period_end=False,
        cancellation_details={"comment": ""},
        **_opcoes(chave),
    )


def buscar_cupom(chave: str, cupom_id: str) -> dict | None:
    """O cupom pelo id, ou `None` se a Stripe não o tem."""
    try:
        return stripe.Coupon.retrieve(cupom_id, **_opcoes(chave)).to_dict()
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) != "resource_missing":
            raise
        return None


def listar_cupons(chave: str) -> list[dict]:
    """Os cupons da Stripe, até `CUPONS_POR_CHAMADA` (os mais recentes)."""
    lista = stripe.Coupon.list(limit=CUPONS_POR_CHAMADA, **_opcoes(chave))
    return [cupom.to_dict() for cupom in lista.data]


def codigo_promocional_novo() -> str:
    sorteio = "".join(secrets.choice(ALFABETO_DO_CODIGO) for _ in range(TAMANHO_DO_CODIGO))
    return f"LEDGR-{sorteio}"


def criar_codigo_promocional(
    chave: str,
    *,
    cupom_id: str,
    cliente_id: str,
    expira_em: datetime,
    conta_id: str,
    operador_id: str,
    chave_de_idempotencia: str,
) -> dict:
    """Um código promocional do cupom só desse cliente, de uma única
    resgatada e com validade. Na API fixada o cupom vai em
    `promotion={"type": "coupon", "coupon": ...}`, e não mais em `coupon`."""
    return stripe.PromotionCode.create(
        promotion={"type": "coupon", "coupon": cupom_id},
        code=codigo_promocional_novo(),
        customer=cliente_id,
        max_redemptions=1,
        expires_at=int(expira_em.timestamp()),
        metadata={"conta_id": conta_id, "operador_id": operador_id},
        **_opcoes(chave, idempotency_key=chave_de_idempotencia),
    ).to_dict()


def aplicar_cupom_na_assinatura(
    chave: str, assinatura: dict, cupom_id: str, chave_de_idempotencia: str
) -> None:
    """Põe o cupom nas próximas faturas da assinatura. `discounts` substitui a
    lista inteira, então os descontos que ela já tem entram de volta (pelo id,
    com `discount`) e o novo vai junto."""
    existentes = [
        {"discount": desconto if isinstance(desconto, str) else desconto["id"]}
        for desconto in assinatura.get("discounts") or []
    ]
    stripe.Subscription.modify(
        assinatura["id"],
        discounts=[*existentes, {"coupon": cupom_id}],
        **_opcoes(chave, idempotency_key=chave_de_idempotencia),
    )
