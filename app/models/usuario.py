import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Usuario(Base, TimestampMixin):
    """Usuário pertence a uma única empresa no MVP (sem RBAC — ver ADR-003).

    Suporta os dois métodos de login decididos na issue #4: credenciais
    (email + senha_hash) e Google OAuth (google_sub). Pelo menos um dos
    dois precisa estar preenchido — ver a CheckConstraint abaixo.
    """

    __tablename__ = "usuarios"
    __table_args__ = (
        CheckConstraint(
            "senha_hash IS NOT NULL OR google_sub IS NOT NULL",
            name="ck_usuarios_tem_metodo_auth",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    senha_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="usuarios")  # noqa: F821
