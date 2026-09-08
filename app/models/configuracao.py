import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Configuracao(Base, TimestampMixin):
    """1:1 com empresa (decisão da issue #4). Guarda os thresholds default do
    motor de matching por empresa, mesmo antes de existir UI para customizar."""

    __tablename__ = "configuracoes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, unique=True
    )
    tolerancia_dias_default: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="2"
    )
    similaridade_minima_default: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="85.00"
    )

    empresa: Mapped["Empresa"] = relationship(back_populates="configuracao")  # noqa: F821
