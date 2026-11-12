"""Webhook da Stripe (issues #166 e #167, ADR-026): a única fonte de
confirmação do pagamento.

`POST /stripe/webhook` é chamado pela própria Stripe, direto no domínio da
API: sem JWT, sem `X-Ledgr-Empresa`, fora do rate limit por IP e sem depender
do `LEDGR_SEGREDO_PROXY`. A proteção é a assinatura: o corpo CRU
(`await request.body()`, sem reserializar) é verificado com
`stripe.Webhook.construct_event` e `STRIPE_WEBHOOK_SECRET`. Assinatura ausente,
inválida ou antiga, ou corpo que não é um evento, é 400 e não grava nada.

- Sem `STRIPE_HABILITADO`, sem a chave ou sem o segredo, 503.
- Idempotência por `eventos_stripe` (app/services/assinatura/webhook.py): o
  evento repetido responde 200 sem refazer nada.
- Erro inesperado é 500, e a Stripe reenvia por até três dias. Sem traceback no
  log: só a categoria.
- O trabalho (banco e chamadas à API da Stripe) roda numa thread, para não
  travar o loop de eventos.

Fica fora do OpenAPI do app: não é uma rota para o front.
"""

import logging
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.assinatura import stripe_api, webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["assinatura"], include_in_schema=False)

ASSINATURA_INVALIDA = "Assinatura do webhook inválida."
ERRO_AO_TRATAR = "Erro ao tratar o evento."


def _tratar(db: Session, evento: dict) -> str:
    try:
        return webhook.tratar_evento(db, evento)
    except Exception as exc:
        # Só a categoria: a mensagem do erro pode trazer valores do payload.
        logger.error(
            "stripe_webhook tipo=%s resultado=erro erro=%s", evento.get("type"), type(exc).__name__
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=ERRO_AO_TRATAR
        ) from exc


@router.post("/webhook")
async def receber_evento(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    assinatura: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, str]:
    segredo = stripe_api.exigir_segredo_do_webhook()
    corpo = await request.body()
    try:
        evento = stripe_api.evento_do_webhook(corpo, assinatura, segredo)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        logger.warning("stripe_webhook resultado=assinatura_invalida")
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=ASSINATURA_INVALIDA
        ) from exc
    resultado = await run_in_threadpool(_tratar, db, evento)
    return {"resultado": resultado}
