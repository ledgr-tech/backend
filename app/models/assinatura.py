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
