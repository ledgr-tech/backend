"""Cobrança pela Stripe (issues #166 e #167, ADR-026): a assinatura da conta, o
registro dos eventos do webhook e os pagamentos (uma linha por fatura).

Quem escreve nestas tabelas é só o webhook (app/services/assinatura/): o
retorno do checkout no navegador não libera nada. Nenhuma guarda dado de
cartão, e `eventos_stripe` guarda só o id, o tipo e as datas do evento, nunca
o payload, que pode ter e-mail e nome.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.plano import PERIODOS, PLANOS
from app.models.base import Base

STATUS_DO_PAGAMENTO = ("paga", "falhou")
# Quem cancelou a assinatura (issue #169): o cliente, a falta de pagamento, o
# operador pelo `/admin` ou direto no painel da Stripe.
ORIGENS_DO_CANCELAMENTO = ("cliente", "falta_de_pagamento", "operador", "painel_stripe")
FORMAS_DO_CUPOM = ("codigo", "assinatura")


def _lista(valores: tuple[str, ...]) -> str:
    return ", ".join(f"'{valor}'" for valor in valores)


class AssinaturaStripe(Base):
    """A assinatura da conta na Stripe, uma por conta. `status` é o da Stripe
    (`active`, `trialing`, `past_due`, `canceled`...), e a situação da conta
    sai dele (app/core/plano.py). `plano` e `periodo` saem da `lookup_key` do
    preço, "<plano>_<periodo>": nenhum preço mora no backend. É sempre
    sincronizada buscando a assinatura de novo na API, nunca só do payload do
    evento, para a ordem de chegada dos eventos não importar."""

    __tablename__ = "assinaturas_stripe"
    __table_args__ = (
        UniqueConstraint("conta_id", name="uq_assinaturas_stripe_conta_id"),
        UniqueConstraint("stripe_subscription_id", name="uq_assinaturas_stripe_subscription_id"),
        CheckConstraint(f"plano IN ({_lista(PLANOS)})", name="ck_assinaturas_stripe_plano_valido"),
        CheckConstraint(
            f"periodo IN ({_lista(PERIODOS)})", name="ck_assinaturas_stripe_periodo_valido"
        ),
        CheckConstraint(
            f"origem_cancelamento IS NULL OR origem_cancelamento IN "
            f"({_lista(ORIGENS_DO_CANCELAMENTO)})",
            name="ck_assinaturas_stripe_origem_cancelamento",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    stripe_subscription_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    plano: Mapped[str] = mapped_column(String, nullable=False)
    periodo: Mapped[str] = mapped_column(String, nullable=False)
    periodo_atual_fim: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelar_no_fim_do_periodo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    cancelada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # O `cancellation_details.reason` da Stripe (`cancellation_requested`,
    # `payment_failed`, `payment_disputed`) e quem cancelou. Só têm valor com a
    # assinatura cancelada ou com o cancelamento agendado (issue #169).
    motivo_cancelamento: Mapped[str | None] = mapped_column(String, nullable=True)
    origem_cancelamento: Mapped[str | None] = mapped_column(String, nullable=True)
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conta: Mapped["Conta"] = relationship(back_populates="assinatura_stripe")  # noqa: F821


class EventoStripe(Base):
    """Evento do webhook já recebido, para idempotência e auditoria. `id` é o
    id do evento na Stripe. `processado_em` nulo é um evento que chegou e ainda
    não terminou de ser tratado (ou falhou e vai ser reenviado)."""

    __tablename__ = "eventos_stripe"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tipo: Mapped[str] = mapped_column(String, nullable=False)
    recebido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    processado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PagamentoStripe(Base):
    """Uma fatura da Stripe da conta: `paga` ou `falhou`. A paga não volta a
    `falhou` se o evento da falha chegar depois (a Stripe não garante a ordem).
    É a base do painel `/admin` da issue #169."""

    __tablename__ = "pagamentos_stripe"
    __table_args__ = (
        UniqueConstraint("stripe_invoice_id", name="uq_pagamentos_stripe_invoice_id"),
        CheckConstraint(
            f"status IN ({_lista(STATUS_DO_PAGAMENTO)})", name="ck_pagamentos_stripe_status"
        ),
        Index("ix_pagamentos_stripe_conta_id_ocorrido_em", "conta_id", "ocorrido_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_invoice_id: Mapped[str] = mapped_column(String, nullable=False)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    valor_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moeda: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    periodo_inicio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    periodo_fim: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ocorrido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CupomEnviado(Base):
    """Cupom que o operador deu a uma conta pelo `/admin` (issue #169, ADR-025
    e ADR-026), só de inserção, menos `email_enviado` e `usado_em`.

    O cupom em si mora na Stripe (criado no painel dela). Aqui ficam o nome e o
    resumo do desconto COPIADOS, para o histórico não depender de o cupom
    continuar lá. `forma` "codigo": um código promocional só daquela conta, de
    uso único, com validade (`codigo`, `stripe_promotion_code_id` e `expira_em`
    preenchidos; `usado_em` vem do webhook do checkout). `forma` "assinatura":
    o desconto foi aplicado direto na assinatura viva, sem código nem validade;
    `usado_em` é o instante da aplicação.

    `operador_id` vira `NULL` se o operador for apagado; o nome fica, como em
    `eventos_conta`. Nada aqui é dado de cartão.
    """

    __tablename__ = "cupons_enviados"
    __table_args__ = (
        UniqueConstraint(
            "stripe_promotion_code_id", name="uq_cupons_enviados_stripe_promotion_code_id"
        ),
        CheckConstraint(f"forma IN ({_lista(FORMAS_DO_CUPOM)})", name="ck_cupons_enviados_forma"),
        CheckConstraint(
            "(forma = 'codigo' AND codigo IS NOT NULL AND stripe_promotion_code_id IS NOT NULL "
            "AND expira_em IS NOT NULL) "
            "OR (forma = 'assinatura' AND codigo IS NULL AND stripe_promotion_code_id IS NULL "
            "AND expira_em IS NULL)",
            name="ck_cupons_enviados_forma_e_codigo",
        ),
        Index("ix_cupons_enviados_conta_id_em", "conta_id", "em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    operador_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "operadores_ledgr.id",
            name="fk_cupons_enviados_operador_id_operadores_ledgr",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    operador_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_coupon_id: Mapped[str] = mapped_column(String, nullable=False)
    cupom_nome: Mapped[str] = mapped_column(String, nullable=False)
    desconto: Mapped[str] = mapped_column(String, nullable=False)
    forma: Mapped[str] = mapped_column(String(10), nullable=False)
    codigo: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_promotion_code_id: Mapped[str | None] = mapped_column(String, nullable=True)
    expira_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_enviado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    usado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
