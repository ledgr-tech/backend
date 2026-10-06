import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Fechamento(Base):
    """Fechamento de uma competência (mês AAAA-MM) da empresa (issue #86).

    A competência de um extrato do banco é o mês de `extratos.periodo_inicio`;
    regra do mês e resumo em `app/services/fechamentos.py`. `resumo` é o
    retrato congelado no momento do fechamento (pares, contagens, pendências),
    para o relatório entregue ao contador não mudar depois.

    Reabrir não apaga: marca `reaberto_*` na linha. O índice único parcial
    garante no máximo um fechamento ATIVO (`reaberto_em IS NULL`) por
    competência; fechar de novo cria outra linha, e o histórico fica guardado.

    Autor no mesmo padrão de `decisoes_linha`: `*_por_id` (vira NULL se o
    usuário for apagado) e uma cópia do nome no momento da ação. Não usa o
    `TimestampMixin`: as datas são `timestamptz`, e `fechado_em` usa
    `clock_timestamp()` (hora real da gravação, depois da trava do mês).
    """

    __tablename__ = "fechamentos"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_fechamentos"),
        CheckConstraint(
            "competencia ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name="ck_fechamentos_competencia_formato",
        ),
        CheckConstraint(
            "(reaberto_em IS NULL AND reaberto_por_nome IS NULL) "
            "OR (reaberto_em IS NOT NULL AND reaberto_por_nome IS NOT NULL)",
            name="ck_fechamentos_reabertura_completa",
        ),
        Index(
            "uq_fechamentos_empresa_id_competencia_ativo",
            "empresa_id",
            "competencia",
            unique=True,
            postgresql_where=text("reaberto_em IS NULL"),
        ),
        Index(
            "ix_fechamentos_empresa_id_competencia_fechado_em",
            "empresa_id",
            "competencia",
            "fechado_em",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("empresas.id", name="fk_fechamentos_empresa_id_empresas"),
        nullable=False,
    )
    competencia: Mapped[str] = mapped_column(String(7), nullable=False)
    ressalva: Mapped[str | None] = mapped_column(Text, nullable=True)
    resumo: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fechado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id", name="fk_fechamentos_fechado_por_id_usuarios", ondelete="SET NULL"
        ),
        nullable=True,
    )
    fechado_por_nome: Mapped[str] = mapped_column(String, nullable=False)
    fechado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    reaberto_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id", name="fk_fechamentos_reaberto_por_id_usuarios", ondelete="SET NULL"
        ),
        nullable=True,
    )
    reaberto_por_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    reaberto_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
