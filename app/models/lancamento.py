import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Lancamento(Base, TimestampMixin):
    """Lançamento normalizado de um extrato, gravado pela camada de
    normalização via BackgroundTasks (issue #12, ver app/services/normalizacao.py).

    `hash_dedup` (sha256 de valor+data+descrição normalizada+extrato_id) e a
    UniqueConstraint(empresa_id, extrato_id, hash_dedup) implementam a
    dedupliação em reprocessamento decidida na ADR-004 (seção multi-tenant e
    constraint de dedup em lançamentos) — via `INSERT ... ON CONFLICT DO
    NOTHING`, não checagem de duplicata na aplicação antes do insert.

    `empresa_id` é indexado (toda query real do sistema filtra por tenant,
    mesma decisão já aplicada em Extrato/Usuario). `extrato_id` ainda não
    tem índice próprio — índices dedicados (extrato_id/data/valor) são
    escopo da issue #15, não antecipados aqui.
    """

    __tablename__ = "lancamentos"
    __table_args__ = (
        CheckConstraint("tipo IN ('credito', 'debito')", name="ck_lancamentos_tipo_valido"),
        UniqueConstraint(
            "empresa_id", "extrato_id", "hash_dedup", name="uq_lancamentos_empresa_extrato_hash"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    extrato_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    data: Mapped[date] = mapped_column(Date, nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    descricao: Mapped[str] = mapped_column(String, nullable=False)
    tipo: Mapped[str] = mapped_column(String, nullable=False)
    hash_dedup: Mapped[str] = mapped_column(String(64), nullable=False)

    extrato: Mapped["Extrato"] = relationship(back_populates="lancamentos")  # noqa: F821
