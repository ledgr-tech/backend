import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Domínio do e-mail que o usuário removido recebe no lugar do dele (issue #69).
# `.invalid` é reservado (RFC 2606): nunca vira endereço de entrega.
DOMINIO_EMAIL_REMOVIDO = "ledgr.invalid"


class Usuario(Base, TimestampMixin):
    """Usuário pertence a uma única empresa (ver ADR-003).

    Suporta os dois métodos de login decididos na issue #4: credenciais
    (email + senha_hash) e Google OAuth (google_sub). Pelo menos um dos
    dois precisa estar preenchido, a não ser no usuário removido — ver a
    CheckConstraint abaixo.

    Sem RBAC, só `administrador` (issue #69, ADR-015): quem cadastrou a
    empresa pelo `/register`. Convidados nunca são. O índice único parcial
    garante no máximo um por empresa.

    Remover um usuário não apaga a linha (`decisoes_linha.usuario_id` aponta
    para ela, e o histórico precisa de quem fez o quê): preenche `removido_em`,
    mantém o nome, apaga senha e `google_sub` e troca o e-mail por
    `removido+<id>@ledgr.invalid`, que libera o endereço para outro cadastro
    ou convite. Regra em app/services/usuarios.py.
    """

    __tablename__ = "usuarios"
    __table_args__ = (
        CheckConstraint(
            "(removido_em IS NULL AND (senha_hash IS NOT NULL OR google_sub IS NOT NULL)) "
            "OR (removido_em IS NOT NULL AND senha_hash IS NULL AND google_sub IS NULL)",
            name="ck_usuarios_tem_metodo_auth",
        ),
        Index(
            "uq_usuarios_empresa_id_administrador",
            "empresa_id",
            unique=True,
            postgresql_where=text("administrador"),
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
    administrador: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    removido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="usuarios")  # noqa: F821
