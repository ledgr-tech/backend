"""Checkout da assinatura pela Stripe (issues #166 e #167, ADR-026).

`POST /assinatura/checkout` cria uma Checkout Session em modo assinatura e
devolve a `url` da página da Stripe, para onde o front redireciona. Só o admin
(403 para os outros, pela dependency `obter_admin_autenticado`), para a conta
toda: ignora `X-Ledgr-Empresa`. Corpo `{plano, periodo}`; valor fora de
`PLANOS` ou `PERIODOS` é 422. Rate limit por conta.

- Sem `STRIPE_HABILITADO` ou sem a chave, 503, e a Stripe não é chamada.
- 409 se a conta já tem assinatura `active`, `trialing`, `past_due` ou
  `incomplete`: trocar de plano é a rota do PR 2.
- O preço é achado pela `lookup_key` "<plano>_<periodo>" (nenhum preço mora no
  backend); sem ele no catálogo, 503.
- O cliente da Stripe é criado uma vez por conta (`contas.stripe_customer_id`),
  com chave de idempotência.
- Quem está no teste só é cobrado no fim dele: `subscription_data.trial_end` é
  `teste_termina_em`. A Stripe exige que o `trial_end` do Checkout esteja a pelo
  menos 48 horas no futuro; faltando menos que isso (ou com o teste já
  encerrado), não há `trial_end` e o primeiro pagamento é na hora, e o que
  sobrava do teste se perde. O front avisa antes de redirecionar.
- Só cartão de crédito. O backend não recebe nem guarda dado de cartão.

Nada aqui libera acesso: a situação da conta só muda quando o webhook
(app/api/stripe_webhook.py) confirma. A rota fica liberada para a conta inativa
(app/core/conta_inativa.py). Logs só com o resultado e ids internos.
"""

import logging
from datetime import timedelta
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import plano as regra
from app.core.auth import obter_admin_autenticado
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import LIMITE_CHECKOUT_POR_CONTA, chave_por_conta, limiter
from app.models import Usuario
from app.services.assinatura import stripe_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assinatura", tags=["assinatura"])

# A Stripe aceita `trial_end` a partir de 48 horas no futuro; o minuto a mais
# cobre o tempo entre calcular aqui e a Stripe receber o pedido.
ANTECEDENCIA_MINIMA_DO_TRIAL = timedelta(hours=48, minutes=1)
# Chamadas repetidas com os mesmos dados nesta janela devolvem a mesma sessão
# (clique duplo); depois dela, uma sessão nova, porque a anterior pode ter
# expirado.
JANELA_DA_IDEMPOTENCIA_SEGUNDOS = 600

JA_ASSINA = (
    "Sua conta já tem uma assinatura. Para trocar de plano ou de cartão, use a área de assinatura."
)
PLANO_INDISPONIVEL = "Esse plano não está disponível no momento."


class CheckoutRequest(BaseModel):
    plano: regra.Plano
    periodo: regra.Periodo


class CheckoutResponse(BaseModel):
    url: str


def _urls_de_retorno() -> tuple[str, str]:
    base = frontend_url_para_link()
    if base is None:
        # Nunca uma URL de retorno quebrada.
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=stripe_api.MENSAGEM_INDISPONIVEL,
        )
    return f"{base}/assinatura?checkout=sucesso", f"{base}/assinatura?checkout=cancelado"


@router.post("/checkout", response_model=CheckoutResponse)
@limiter.limit(LIMITE_CHECKOUT_POR_CONTA, key_func=chave_por_conta)
def iniciar_checkout(
    request: Request,
    dados: CheckoutRequest,
    admin: Annotated[Usuario, Depends(obter_admin_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> CheckoutResponse:
    chave = stripe_api.exigir_stripe()
    success_url, cancel_url = _urls_de_retorno()
    conta = admin.conta
    assinatura = conta.assinatura_stripe
    if assinatura is not None and assinatura.status in regra.STATUS_EM_ANDAMENTO:
        logger.info("checkout resultado=ja_assina conta_id=%s", conta.id)
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=JA_ASSINA)

    agora = regra.agora()
    fim_do_teste = conta.teste_termina_em
    trial_end = (
        int(fim_do_teste.timestamp())
        if fim_do_teste is not None and fim_do_teste - agora >= ANTECEDENCIA_MINIMA_DO_TRIAL
        else None
    )
    conta_id = str(conta.id)
    try:
        preco = stripe_api.buscar_preco(chave, stripe_api.lookup_key(dados.plano, dados.periodo))
        if preco is None:
            logger.error(
                "checkout resultado=preco_ausente plano=%s periodo=%s", dados.plano, dados.periodo
            )
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=PLANO_INDISPONIVEL
            )
        cliente_id = conta.stripe_customer_id
        if cliente_id is None:
            cliente_id = stripe_api.criar_cliente(
                chave,
                conta_id=conta_id,
                razao_social=conta.razao_social,
                cnpj=conta.cnpj,
                email=admin.email,
            )
            conta.stripe_customer_id = cliente_id
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise
        janela = int(agora.timestamp()) // JANELA_DA_IDEMPOTENCIA_SEGUNDOS
        url = stripe_api.criar_sessao_de_checkout(
            chave,
            cliente_id=cliente_id,
            preco_id=preco["id"],
            conta_id=conta_id,
            trial_end=trial_end,
            success_url=success_url,
            cancel_url=cancel_url,
            chave_de_idempotencia=(
                f"ledgr-checkout-{conta_id}-{dados.plano}-{dados.periodo}"
                f"-{'trial' if trial_end else 'agora'}-{janela}"
            ),
        )
    except stripe.StripeError as exc:
        raise stripe_api.erro_da_stripe(exc, "checkout") from exc
    logger.info(
        "checkout resultado=criado conta_id=%s plano=%s periodo=%s trial=%s",
        conta_id,
        dados.plano,
        dados.periodo,
        trial_end is not None,
    )
    return CheckoutResponse(url=url)
