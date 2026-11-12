"""O que o operador do Ledgr faz na assinatura de uma conta pelo `/admin`
(issue #169, ADR-025 e ADR-026): cancelar, desfazer o cancelamento agendado e
dar um cupom.

Chama a Stripe, e por isso só as ações passam por aqui; as telas de consulta
leem o que o webhook guardou (assinaturas_stripe, pagamentos_stripe). Depois de
cada ação a assinatura é sincronizada na hora, pela mesma função do webhook
(`sincronizar_assinatura`), para o painel já mostrar o estado novo; o webhook
chega depois e é idempotente.

O que NÃO faz: reembolso e crédito proporcional. O cancelamento "agora" não gera
fatura final nem proporcional; devolver dinheiro é no painel da Stripe. Nenhum
dado de cartão passa por aqui. Quem chama faz o commit e grava o evento da
conta; `stripe.StripeError` sobe para a rota virar 502.

Logs só com ids internos e o resultado, nunca e-mail, nome, código ou texto do
operador.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.plano import agora
from app.models import AssinaturaStripe, Conta, CupomEnviado
from app.services.assinatura import cupons, sincronizacao, stripe_api
from app.services.assinatura.cliente import garantir_cliente
from app.services.email import ProvedorDeEmail
from app.services.email.base import MensagemEmail

logger = logging.getLogger(__name__)

Quando = Literal["fim_do_periodo", "agora"]

# `active`, `trialing` e `past_due`: a assinatura ainda está de pé, e por isso
# o checkout dá 409 para a conta. `incomplete` não conta.
STATUS_VIVOS = frozenset({"active", "trialing", "past_due"})

SEM_ASSINATURA_VIVA = "A conta não tem uma assinatura ativa."
JA_AGENDADO = "O cancelamento já está agendado para o fim do período."
SEM_CANCELAMENTO_AGENDADO = "A assinatura não tem cancelamento agendado."
CUPOM_NAO_ENCONTRADO = "Cupom não encontrado ou não está mais válido."
SEM_ADMIN = "A conta não tem um administrador ativo para receber o cupom."


def _conflito(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detalhe)


def assinatura_viva(conta: Conta) -> AssinaturaStripe | None:
    assinatura = conta.assinatura_stripe
    return assinatura if assinatura is not None and assinatura.status in STATUS_VIVOS else None


def _sincronizar(db: Session, assinatura: AssinaturaStripe, acao: str) -> None:
    resultado = sincronizacao.sincronizar_assinatura(db, assinatura.stripe_subscription_id)
    logger.info("admin_assinatura acao=%s sincronizacao=%s", acao, resultado)
    db.refresh(assinatura)


def cancelar_assinatura(
    db: Session, chave: str, conta: Conta, quando: Quando
) -> Literal["assinatura_cancelada", "cancelamento_agendado"]:
    """Cancela a assinatura da conta (travada por quem chama) e sincroniza.
    Devolve o tipo do evento da conta. 409 sem assinatura viva e, no fim do
    período, se o cancelamento já está agendado."""
    assinatura = assinatura_viva(conta)
    if assinatura is None:
        raise _conflito(SEM_ASSINATURA_VIVA)
    if quando == "fim_do_periodo":
        if assinatura.cancelar_no_fim_do_periodo:
            raise _conflito(JA_AGENDADO)
        stripe_api.cancelar_assinatura_no_fim_do_periodo(chave, assinatura.stripe_subscription_id)
        tipo = "cancelamento_agendado"
    else:
        stripe_api.cancelar_assinatura_agora(chave, assinatura.stripe_subscription_id)
        tipo = "assinatura_cancelada"
    _sincronizar(db, assinatura, tipo)
    return tipo


def manter_assinatura(db: Session, chave: str, conta: Conta) -> None:
    """Desfaz o cancelamento agendado (a conta travada por quem chama) e
    sincroniza. 409 sem assinatura viva ou sem cancelamento agendado."""
    assinatura = assinatura_viva(conta)
    if assinatura is None:
        raise _conflito(SEM_ASSINATURA_VIVA)
    if not assinatura.cancelar_no_fim_do_periodo:
        raise _conflito(SEM_CANCELAMENTO_AGENDADO)
    stripe_api.manter_assinatura(chave, assinatura.stripe_subscription_id)
    _sincronizar(db, assinatura, "cancelamento_desfeito")


def dar_cupom(
    db: Session,
    chave: str,
    conta: Conta,
    *,
    admin_email: str,
    cupom_id: str,
    validade_dias: int,
    operador_id: uuid.UUID,
    operador_nome: str,
) -> CupomEnviado:
    """Dá o cupom à conta e põe o registro em `db` (sem commit).

    Com assinatura viva, o desconto vai direto nela, nas próximas faturas, sem
    código (o checkout dá 409 para quem já assina). Sem ela, cria um código
    promocional só do Customer da conta (criado aqui se ainda não existe), de
    uma resgatada e com validade; a validade nunca passa do `redeem_by` do
    cupom. 404 se o cupom não existe ou não é mais válido."""
    cupom = stripe_api.buscar_cupom(chave, cupom_id)
    if not cupons.cupom_valido(cupom):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=CUPOM_NAO_ENCONTRADO)
    token = uuid.uuid4().hex
    registro = CupomEnviado(
        conta_id=conta.id,
        operador_id=operador_id,
        operador_nome=operador_nome,
        stripe_coupon_id=cupom["id"],
        cupom_nome=cupons.nome_do_cupom(cupom),
        desconto=cupons.resumo_do_desconto(cupom),
    )
    assinatura = assinatura_viva(conta)
    if assinatura is not None:
        atual = stripe_api.buscar_assinatura(chave, assinatura.stripe_subscription_id)
        stripe_api.aplicar_cupom_na_assinatura(
            chave, atual, cupom["id"], f"ledgr-cupom-assinatura-{conta.id}-{cupom['id']}-{token}"
        )
        registro.forma = "assinatura"
        registro.usado_em = func.now()
    else:
        cliente_id = garantir_cliente(db, chave, conta, admin_email)
        expira_em = agora() + timedelta(days=validade_dias)
        if cupom.get("redeem_by"):
            expira_em = min(expira_em, datetime.fromtimestamp(cupom["redeem_by"], UTC))
        codigo = stripe_api.criar_codigo_promocional(
            chave,
            cupom_id=cupom["id"],
            cliente_id=cliente_id,
            expira_em=expira_em,
            conta_id=str(conta.id),
            operador_id=str(operador_id),
            chave_de_idempotencia=f"ledgr-cupom-codigo-{conta.id}-{cupom['id']}-{token}",
        )
        registro.forma = "codigo"
        registro.codigo = codigo["code"]
        registro.stripe_promotion_code_id = codigo["id"]
        registro.expira_em = datetime.fromtimestamp(codigo["expires_at"], UTC)
    db.add(registro)
    logger.info("admin_cupom resultado=criado conta_id=%s forma=%s", conta.id, registro.forma)
    return registro


def enviar_email_do_cupom(provedor: ProvedorDeEmail | None, mensagem: MensagemEmail) -> bool:
    """Envia o e-mail do cupom e diz se saiu. Sem provedor (e-mail desligado),
    ou com o envio falhando, é `False`: o cupom já existe e o operador copia o
    código da resposta."""
    if provedor is None:
        return False
    try:
        provedor.enviar(mensagem)
    except Exception as exc:  # noqa: BLE001 - o cupom não se desfaz por e-mail
        logger.info("admin_cupom email=falha_envio classe=%s", type(exc).__name__)
        return False
    return True
