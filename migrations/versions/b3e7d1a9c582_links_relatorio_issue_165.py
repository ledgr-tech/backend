"""links públicos do relatório do fechamento (issue #165, ADR-024)

Tabela `links_relatorio`: um link por linha, preso ao fechamento, com o sha256
do token (único), o prefixo, quem criou, a validade, quem revogou e por quê
(`manual` ou `reabertura`) e a contagem de acessos.

Revision ID: b3e7d1a9c582
Revises: a8d2f6c4e913
Create Date: 2026-10-28 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3e7d1a9c582"
down_revision: str | None = "a8d2f6c4e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "links_relatorio",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fechamento_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prefixo", sa.String(length=8), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("criado_por_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("criado_por_nome", sa.String(), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revogado_por_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revogado_por_nome", sa.String(), nullable=True),
        sa.Column("revogado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_revogacao", sa.String(length=10), nullable=True),
        sa.Column("ultimo_acesso_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acessos", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "motivo_revogacao IN ('manual', 'reabertura')", name="ck_links_relatorio_motivo_valido"
        ),
        sa.CheckConstraint(
            "(revogado_em IS NULL AND revogado_por_nome IS NULL AND motivo_revogacao IS NULL) "
            "OR (revogado_em IS NOT NULL AND revogado_por_nome IS NOT NULL "
            "AND motivo_revogacao IS NOT NULL)",
            name="ck_links_relatorio_revogacao_completa",
        ),
        sa.CheckConstraint("acessos >= 0", name="ck_links_relatorio_acessos_positivos"),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.ForeignKeyConstraint(["fechamento_id"], ["fechamentos.id"]),
        sa.ForeignKeyConstraint(
            ["criado_por_id"],
            ["usuarios.id"],
            name="fk_links_relatorio_criado_por_id_usuarios",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["revogado_por_id"],
            ["usuarios.id"],
            name="fk_links_relatorio_revogado_por_id_usuarios",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index(
        op.f("ix_links_relatorio_empresa_id"), "links_relatorio", ["empresa_id"], unique=False
    )
    op.create_index(
        "ix_links_relatorio_fechamento_id_criado_em",
        "links_relatorio",
        ["fechamento_id", "criado_em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_links_relatorio_fechamento_id_criado_em", table_name="links_relatorio")
    op.drop_index(op.f("ix_links_relatorio_empresa_id"), table_name="links_relatorio")
    op.drop_table("links_relatorio")
