"""Grava a assinatura e os pagamentos da conta a partir da Stripe (issues #166
e #167, ADR-026).

A assinatura é SEMPRE sincronizada buscando a Subscription de novo na API
(`sincronizar_assinatura`), nunca só do payload do evento: a Stripe não
garante a ordem dos eventos, e um `updated` atrasado não pode desfazer um
`deleted`. O plano e o período saem da `lookup_key` do preço.

A conta é achada pela metadata `conta_id` (que o checkout põe na assinatura) ou,
na falta dela, pelo cliente (`contas.stripe_customer_id`). Assinatura de preço
que não é do Ledgr, ou sem conta, é ignorada e logada.

Os pagamentos (uma linha por fatura) vêm do payload da fatura: só campos
estáveis entre versões da API. Uma fatura `paga` não volta a `falhou` se o
evento da falha chegar depois.

Cada função trava a linha da conta, para dois eventos da mesma conta chegando
juntos não se atropelarem. Quem chama faz o commit. Logs só com categoria e
ids internos, nunca e-mail, nome, CNPJ nem o payload.
"""

import logging
import uuid
from datetime import UTC, datetime

import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.plano import STATUS_EM_ANDAMENTO
from app.models import AssinaturaStripe, Conta, PagamentoStripe
from app.services.assinatura import stripe_api

logger = logging.getLogger(__name__)


def _instante(timestamp: int | None) -> datetime | None:
    return datetime.fromtimestamp(timestamp, UTC) if timestamp else None


def _uuid(valor: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return None


def _id(valor: object) -> str | None:
    """O id de um campo que vem como string ou, expandido, como objeto."""
    if isinstance(valor, dict):
        valor = valor.get("id")
    return valor if isinstance(valor, str) and valor else None


def _conta_travada(db: Session, conta_id: uuid.UUID | None, cliente_id: str | None) -> Conta | None:
    """A conta pelo id da metadata ou, na falta, pelo cliente da Stripe, com a
    linha travada."""
    condicao = None
    if conta_id is not None:
        condicao = Conta.id == conta_id
    elif cliente_id is not None:
        condicao = Conta.stripe_customer_id == cliente_id
    if condicao is None:
        return None
    return db.scalar(
        select(Conta).where(condicao).with_for_update().execution_options(populate_existing=True)
    )


def _ligar_cliente(conta: Conta, cliente_id: str | None) -> None:
    if cliente_id is None:
        return
    if conta.stripe_customer_id is None:
        conta.stripe_customer_id = cliente_id
    elif conta.stripe_customer_id != cliente_id:
        logger.warning("assinatura resultado=cliente_diferente conta_id=%s", conta.id)


def _itens(assinatura: dict) -> list[dict]:
    return ((assinatura.get("items") or {}).get("data")) or []


def _preco_do_ledgr(assinatura: dict) -> tuple[dict, str, str] | None:
    """O item da assinatura cujo preço tem `lookup_key` do Ledgr, com o plano
    e o período dela."""
    for item in _itens(assinatura):
        par = stripe_api.plano_e_periodo((item.get("price") or {}).get("lookup_key"))
        if par is not None:
            return item, par[0], par[1]
    return None


def aplicar_assinatura(db: Session, assinatura: dict) -> str:
    """Grava `assinaturas_stripe` a partir da Subscription (já buscada na API).
    Devolve o resultado categorizado."""
    conta_id = _uuid((assinatura.get("metadata") or {}).get("conta_id"))
    cliente_id = _id(assinatura.get("customer"))
    conta = _conta_travada(db, conta_id, cliente_id)
    if conta is None:
        logger.warning("assinatura resultado=sem_conta")
        return "sem_conta"
    preco = _preco_do_ledgr(assinatura)
    if preco is None:
        logger.warning("assinatura resultado=preco_desconhecido conta_id=%s", conta.id)
        return "preco_desconhecido"
    item, plano, periodo = preco
    _ligar_cliente(conta, cliente_id)

    atual = db.scalar(select(AssinaturaStripe).where(AssinaturaStripe.conta_id == conta.id))
    id_da_assinatura = assinatura["id"]
    status = assinatura["status"]
    # Outra assinatura da conta: só substitui a que já terminou, ou quando esta
    # também está andando. Um evento atrasado de uma assinatura antiga
    # (encerrada) não derruba a nova.
    if (
        atual is not None
        and atual.stripe_subscription_id != id_da_assinatura
        and atual.status in STATUS_EM_ANDAMENTO
        and status not in STATUS_EM_ANDAMENTO
    ):
        logger.info("assinatura resultado=evento_de_assinatura_antiga conta_id=%s", conta.id)
        return "assinatura_antiga"
    if atual is None:
        atual = AssinaturaStripe(conta_id=conta.id)
        db.add(atual)
    atual.stripe_subscription_id = id_da_assinatura
    atual.status = status
    atual.plano = plano
    atual.periodo = periodo
    # Nas versões recentes da API, o fim do período fica no item da assinatura.
    atual.periodo_atual_fim = _instante(
        item.get("current_period_end") or assinatura.get("current_period_end")
    )
    atual.cancelar_no_fim_do_periodo = bool(assinatura.get("cancel_at_period_end"))
    atual.cancelada_em = _instante(assinatura.get("canceled_at"))
    atual.atualizada_em = func.now()
    db.flush()
    logger.info(
        "assinatura resultado=sincronizada conta_id=%s status=%s plano=%s periodo=%s",
        conta.id,
        status,
        plano,
        periodo,
    )
    return "sincronizada"


def sincronizar_assinatura(db: Session, subscription_id: str) -> str:
    """Busca a assinatura na API da Stripe e a grava. Assinatura que não existe
    mais na Stripe (resource_missing) é ignorada, para a Stripe não reenviar o
    evento por três dias."""
    chave = stripe_api.exigir_stripe()
    try:
        assinatura = stripe_api.buscar_assinatura(chave, subscription_id)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) != "resource_missing":
            raise
        logger.warning("assinatura resultado=nao_existe_na_stripe")
        return "nao_existe"
    return aplicar_assinatura(db, assinatura)


def concluir_checkout(db: Session, sessao: dict) -> str:
    """`checkout.session.completed`: liga o cliente e a assinatura à conta pelo
    `client_reference_id` (a assinatura é buscada de novo na API)."""
    if sessao.get("mode") != "subscription":
        return "ignorado"
    conta_id = _uuid(sessao.get("client_reference_id")) or _uuid(
        (sessao.get("metadata") or {}).get("conta_id")
    )
    cliente_id = _id(sessao.get("customer"))
    conta = _conta_travada(db, conta_id, None)
    if conta is None:
        logger.warning("checkout resultado=sem_conta")
        return "sem_conta"
    _ligar_cliente(conta, cliente_id)
    db.flush()
    subscription_id = _id(sessao.get("subscription"))
    if subscription_id is None:
        return "sem_assinatura"
    return sincronizar_assinatura(db, subscription_id)


def _periodo_da_fatura(fatura: dict) -> tuple[datetime | None, datetime | None]:
    """O período de serviço da fatura: o da primeira linha; senão, o da fatura."""
    linhas = (fatura.get("lines") or {}).get("data") or []
    periodo = (linhas[0].get("period") if linhas else None) or {}
    inicio = periodo.get("start") or fatura.get("period_start")
    fim = periodo.get("end") or fatura.get("period_end")
    return _instante(inicio), _instante(fim)


def _conta_id_da_fatura(fatura: dict) -> uuid.UUID | None:
    """A conta da metadata da assinatura que gerou a fatura: `parent` nas
    versões recentes da API, `subscription_details` nas anteriores."""
    detalhes = ((fatura.get("parent") or {}).get("subscription_details")) or fatura.get(
        "subscription_details"
    )
    return _uuid(((detalhes or {}).get("metadata") or {}).get("conta_id"))


def registrar_pagamento(db: Session, fatura: dict, status: str, ocorrido_em: datetime) -> str:
    """Upsert em `pagamentos_stripe` (uma linha por fatura). `status` é `paga`
    ou `falhou`."""
    fatura_id = fatura.get("id")
    conta = _conta_travada(db, _conta_id_da_fatura(fatura), _id(fatura.get("customer")))
    if conta is None or not fatura_id:
        logger.warning("pagamento resultado=sem_conta")
        return "sem_conta"
    valor = fatura.get("amount_paid") if status == "paga" else fatura.get("amount_due")
    inicio, fim = _periodo_da_fatura(fatura)
    pagamento = db.scalar(
        select(PagamentoStripe).where(PagamentoStripe.stripe_invoice_id == fatura_id)
    )
    if pagamento is None:
        pagamento = PagamentoStripe(stripe_invoice_id=fatura_id, conta_id=conta.id)
        db.add(pagamento)
    elif pagamento.status == "paga" and status == "falhou":
        # A falha chegou depois do pagamento (retentativa): a paga é definitiva.
        logger.info("pagamento resultado=falha_atrasada conta_id=%s", conta.id)
        return "falha_atrasada"
    pagamento.valor_centavos = valor or 0
    pagamento.moeda = (fatura.get("currency") or "brl").lower()
    pagamento.status = status
    pagamento.periodo_inicio = inicio
    pagamento.periodo_fim = fim
    pagamento.ocorrido_em = ocorrido_em
    db.flush()
    logger.info("pagamento resultado=%s conta_id=%s", status, conta.id)
    return status
