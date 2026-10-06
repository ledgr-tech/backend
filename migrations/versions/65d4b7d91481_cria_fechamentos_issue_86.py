"""cria fechamentos (issue #86)

Revision ID: 65d4b7d91481
Revises: 9d2d9979f63b
Create Date: 2026-10-06 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "65d4b7d91481"
down_revision: str | None = "9d2d9979f63b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fechamentos",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("competencia", sa.String(length=7), nullable=False),
        sa.Column("ressalva", sa.Text(), nullable=True),
        sa.Column("resumo", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fechado_por_id", sa.UUID(), nullable=True),
        sa.Column("fechado_por_nome", sa.String(), nullable=False),
        # clock_timestamp(), não now(): a hora real da gravação, feita depois
        # da trava do mês (mesma razão de decisoes_linha.em).
        sa.Column(
            "fechado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("reaberto_por_id", sa.UUID(), nullable=True),
        sa.Column("reaberto_por_nome", sa.String(), nullable=True),
        sa.Column("reaberto_em", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "competencia ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name="ck_fechamentos_competencia_formato",
        ),
        sa.CheckConstraint(
            "(reaberto_em IS NULL AND reaberto_por_nome IS NULL) "
            "OR (reaberto_em IS NOT NULL AND reaberto_por_nome IS NOT NULL)",
            name="ck_fechamentos_reabertura_completa",
        ),
        sa.ForeignKeyConstraint(
            ["empresa_id"], ["empresas.id"], name="fk_fechamentos_empresa_id_empresas"
        ),
        sa.ForeignKeyConstraint(
            ["fechado_por_id"],
            ["usuarios.id"],
            name="fk_fechamentos_fechado_por_id_usuarios",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reaberto_por_id"],
            ["usuarios.id"],
            name="fk_fechamentos_reaberto_por_id_usuarios",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fechamentos"),
    )
    op.create_index(
        "uq_fechamentos_empresa_id_competencia_ativo",
        "fechamentos",
        ["empresa_id", "competencia"],
        unique=True,
        postgresql_where=sa.text("reaberto_em IS NULL"),
    )
    op.create_index(
        "ix_fechamentos_empresa_id_competencia_fechado_em",
        "fechamentos",
        ["empresa_id", "competencia", "fechado_em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fechamentos_empresa_id_competencia_fechado_em", table_name="fechamentos")
    op.drop_index("uq_fechamentos_empresa_id_competencia_ativo", table_name="fechamentos")
    op.drop_table("fechamentos")
