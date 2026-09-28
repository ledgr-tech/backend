import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

FINALIDADE_RECUPERACAO_SENHA = "recuperacao_senha"
FINALIDADE_TROCA_EMAIL = "troca_email"


class TokenEmail(Base, TimestampMixin):
    """Token de uso único enviado por e-mail (issue #66, ADR-012): link de
    recuperação de senha e, depois, de aprovação da troca de e-mail.

    O token em si nunca é gravado, só `token_hash`, o sha256 hex. Como o
    token é aleatório e longo (`secrets.token_urlsafe(32)`), sha256 basta e
    permite buscar pelo hash — bcrypt não é necessário. Quem tiver acesso
    ao banco não consegue montar um link válido.

    `usado_em` marca tanto o token consumido quanto o substituído por um
    pedido novo da mesma finalidade: em ambos os casos ele deixa de valer.
    `expira_em` e `usado_em` são `timestamptz` e sempre comparados com o
    `now()` do Postgres. O índice (`usuario_id`, `finalidade`, `criado_em`)
    serve à contagem de envios por hora (rate limit por destino) e à busca
    dos pedidos pendentes do usuário.

    FK com `ON DELETE CASCADE`: um token não tem valor sem o usuário, e
    apagar a conta (LGPD) leva os tokens junto.
    """

    __tablename__ = "tokens_email"
    __table_args__ = (
        CheckConstraint(
            f"finalidade IN ('{FINALIDADE_RECUPERACAO_SENHA}', '{FINALIDADE_TROCA_EMAIL}')",
            name="ck_tokens_email_finalidade",
        ),
        Index(
            "ix_tokens_email_usuario_id_finalidade_criado_em",
            "usuario_id",
            "finalidade",
            "criado_em",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False
    )
    finalidade: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
