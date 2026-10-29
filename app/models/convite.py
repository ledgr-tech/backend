import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.usuario import PAPEIS_SEM_ADMIN


class Convite(Base, TimestampMixin):
    """Convite para entrar numa conta que já existe (issue #69, ADR-015; da
    conta, e não mais da empresa, desde a issue #133, ADR-019).

    Não reaproveita `tokens_email`: lá o token pertence a um usuário que já
    existe (`usuario_id` NOT NULL), e o convidado ainda não tem conta.

    O token segue o desenho do ADR-012: só `token_hash` (sha256 hex) é
    gravado, de um `secrets.token_urlsafe(32)`. `email` é o normalizado
    (`normalizar_email`) e vira o e-mail do usuário criado no aceite.

    Pendente = `usado_em` e `cancelado_em` nulos e `expira_em` no futuro.
    `cancelado_em` marca tanto o cancelamento pelo administrador quanto o
    convite substituído por outro para o mesmo e-mail na mesma conta. As
    três datas são `timestamptz`, comparadas com o `now()` do Postgres. O
    índice (`conta_id`, `criado_em`) serve à contagem de envios por hora.

    `papel` (issue #133, ADR-019) é o papel que o convidado recebe no aceite:
    'gestor' ou 'analista', nunca 'admin' (o admin só muda por transferência).
    Os convites anteriores à #133 receberam 'gestor' na migração.

    `convidado_por_id` vira NULL se o usuário for apagado (o mesmo padrão de
    `fechamentos.fechado_por_id`); remover pela rota não apaga a linha.
    """

    __tablename__ = "convites"
    __table_args__ = (
        CheckConstraint(
            "usado_em IS NULL OR cancelado_em IS NULL",
            name="ck_convites_usado_ou_cancelado",
        ),
        CheckConstraint(
            "papel IN (" + ", ".join(f"'{papel}'" for papel in PAPEIS_SEM_ADMIN) + ")",
            name="ck_convites_papel_valido",
        ),
        Index("ix_convites_conta_id_criado_em", "conta_id", "criado_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    papel: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    convidado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
