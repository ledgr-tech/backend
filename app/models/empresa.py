import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Empresa(Base, TimestampMixin):
    """O tenant do sistema. Toda tabela com dado de negócio se escopa por empresa_id
    (ver ADR-004, seção multi-tenant — issue #5)."""

    __tablename__ = "empresas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    razao_social: Mapped[str] = mapped_column(String, nullable=False)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False, unique=True)

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="empresa")  # noqa: F821
    configuracao: Mapped["Configuracao"] = relationship(  # noqa: F821
        back_populates="empresa", uselist=False
    )
