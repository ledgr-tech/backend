import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

MOTIVOS_DE_REVOGACAO = ("manual", "reabertura")


class LinkRelatorio(Base):
    """Link público do relatório de um fechamento (issue #165, ADR-024).

    Preso ao fechamento (`fechamento_id`), não à competência: reabrir o mês
    revoga os links daquele fechamento (`motivo_revogacao` "reabertura"), e
    fechar de novo pede link novo. O token (32 bytes aleatórios) nunca é
    gravado: só o sha256 (`hash`, único) e os 8 primeiros caracteres
    (`prefixo`), para a listagem.

    Quem criou e quem revogou, com a cópia do nome, no padrão de
    `fechamentos`: o id vira `NULL` se o usuário for apagado. `acessos` e
    `ultimo_acesso_em` contam as consultas válidas, sem guardar IP.

    Não usa o `TimestampMixin`: as datas são `timestamptz`.
    """

    __tablename__ = "links_relatorio"
    __table_args__ = (
        CheckConstraint(
            "motivo_revogacao IN (" + ", ".join(f"'{m}'" for m in MOTIVOS_DE_REVOGACAO) + ")",
            name="ck_links_relatorio_motivo_valido",
        ),
        CheckConstraint(
            "(revogado_em IS NULL AND revogado_por_nome IS NULL AND motivo_revogacao IS NULL) "
            "OR (revogado_em IS NOT NULL AND revogado_por_nome IS NOT NULL "
            "AND motivo_revogacao IS NOT NULL)",
            name="ck_links_relatorio_revogacao_completa",
        ),
        CheckConstraint("acessos >= 0", name="ck_links_relatorio_acessos_positivos"),
        Index("ix_links_relatorio_fechamento_id_criado_em", "fechamento_id", "criado_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    fechamento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fechamentos.id"), nullable=False
    )
    prefixo: Mapped[str] = mapped_column(String(8), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    criado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id", name="fk_links_relatorio_criado_por_id_usuarios", ondelete="SET NULL"
        ),
        nullable=True,
    )
    criado_por_nome: Mapped[str] = mapped_column(String, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revogado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id", name="fk_links_relatorio_revogado_por_id_usuarios", ondelete="SET NULL"
        ),
        nullable=True,
    )
    revogado_por_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    revogado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    motivo_revogacao: Mapped[str | None] = mapped_column(String(10), nullable=True)
    ultimo_acesso_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acessos: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
