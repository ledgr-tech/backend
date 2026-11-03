"""O Customer da Stripe da conta, criado uma vez e guardado em
`contas.stripe_customer_id` (issues #166, #167 e #169, ADR-026).

Usado pelo checkout da assinatura e pelo cupom do `/admin`, para os dois
falarem do mesmo cliente.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Conta
from app.services.assinatura import stripe_api


def garantir_cliente(db: Session, chave: str, conta: Conta, email: str) -> str:
    """O id do Customer da conta; se ainda não existe, cria na Stripe (com chave
    de idempotência) e faz o commit do id logo, para um erro adiante não perder
    o cliente criado. `stripe.StripeError` sobe para quem chama."""
    if conta.stripe_customer_id is not None:
        return conta.stripe_customer_id
    cliente_id = stripe_api.criar_cliente(
        chave,
        conta_id=str(conta.id),
        razao_social=conta.razao_social,
        cnpj=conta.cnpj,
        email=email,
    )
    conta.stripe_customer_id = cliente_id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    return cliente_id
