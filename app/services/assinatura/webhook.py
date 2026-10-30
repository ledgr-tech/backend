"""O tratamento dos eventos do webhook da Stripe (issues #166 e #167, ADR-026).

`tratar_evento` recebe o evento já verificado (a rota confere a assinatura
sobre o corpo cru) e faz tudo numa transação só:

1. insere o id em `eventos_stripe` (ON CONFLICT DO NOTHING) e trava a linha: um
   evento duplicado, ou reenviado enquanto o primeiro ainda roda, espera e
   depois vê `processado_em` preenchido e responde sem refazer;
2. chama o tratador do tipo do evento;
3. marca `processado_em` e faz o commit.

Qualquer erro desfaz tudo (inclusive a linha do evento) e sobe: a rota
responde 500 e a Stripe reenvia. Tipos que o Ledgr não trata só ficam
registrados.

Tratados: `checkout.session.completed`, `customer.subscription.created`,
`.updated`, `.deleted`, `invoice.paid` e `invoice.payment_failed`.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import EventoStripe
from app.services.assinatura import sincronizacao

logger = logging.getLogger(__name__)


def _checkout_concluido(db: Session, evento: dict) -> str:
    return sincronizacao.concluir_checkout(db, evento["data"]["object"])


def _assinatura_alterada(db: Session, evento: dict) -> str:
    # Só o id: a assinatura é buscada de novo na API.
    return sincronizacao.sincronizar_assinatura(db, evento["data"]["object"]["id"])


def _fatura_paga(db: Session, evento: dict) -> str:
    fatura = evento["data"]["object"]
    transicoes = fatura.get("status_transitions") or {}
    instante = transicoes.get("paid_at") or evento.get("created")
    return sincronizacao.registrar_pagamento(
        db, fatura, "paga", datetime.fromtimestamp(instante, UTC)
    )


def _fatura_falhou(db: Session, evento: dict) -> str:
    return sincronizacao.registrar_pagamento(
        db, evento["data"]["object"], "falhou", datetime.fromtimestamp(evento["created"], UTC)
    )


TRATADORES: dict[str, Callable[[Session, dict], str]] = {
    "checkout.session.completed": _checkout_concluido,
    "customer.subscription.created": _assinatura_alterada,
    "customer.subscription.updated": _assinatura_alterada,
    "customer.subscription.deleted": _assinatura_alterada,
    "invoice.paid": _fatura_paga,
    "invoice.payment_failed": _fatura_falhou,
}


def tratar_evento(db: Session, evento: dict) -> str:
    """Trata o evento e devolve o resultado: `duplicado`, `ignorado` ou o do
    tratador. O commit é daqui."""
    evento_id, tipo = evento["id"], evento["type"]
    try:
        db.execute(
            insert(EventoStripe)
            .values(id=evento_id, tipo=tipo)
            .on_conflict_do_nothing(index_elements=[EventoStripe.id])
        )
        processado_em = db.scalar(
            select(EventoStripe.processado_em)
            .where(EventoStripe.id == evento_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if processado_em is not None:
            db.rollback()
            logger.info("stripe_webhook tipo=%s resultado=duplicado", tipo)
            return "duplicado"
        tratador = TRATADORES.get(tipo)
        resultado = tratador(db, evento) if tratador else "ignorado"
        db.execute(
            update(EventoStripe)
            .where(EventoStripe.id == evento_id)
            .values(processado_em=func.now())
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    logger.info("stripe_webhook tipo=%s resultado=%s", tipo, resultado)
    return resultado
