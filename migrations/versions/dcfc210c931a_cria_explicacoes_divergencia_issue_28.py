"""cria explicacoes_divergencia (issue #28, ADR-011)

Revision ID: dcfc210c931a
Revises: a0d81fa901ae
Create Date: 2026-09-24 11:15:08.669044

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dcfc210c931a"
down_revision: str | None = "a0d81fa901ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "explicacoes_divergencia",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("chave", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provedor", sa.String(), nullable=False),
        sa.Column("modelo", sa.String(), nullable=False),
        sa.Column("versao_prompt", sa.String(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("tokens_entrada", sa.Integer(), nullable=False),
        sa.Column("tokens_saida", sa.Integer(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["empresa_id"],
            ["empresas.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "empresa_id", "chave", name="uq_explicacoes_divergencia_empresa_id_chave"
        ),
    )
    op.create_index(
        op.f("ix_explicacoes_divergencia_empresa_id"),
        "explicacoes_divergencia",
        ["empresa_id"],
        unique=False,
    )
    op.create_index(
        "ix_explicacoes_divergencia_empresa_id_criado_em",
        "explicacoes_divergencia",
        ["empresa_id", "criado_em"],
        unique=False,
    )
    op.create_index(
        "ix_explicacoes_divergencia_criado_em",
        "explicacoes_divergencia",
        ["criado_em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_explicacoes_divergencia_criado_em", table_name="explicacoes_divergencia")
    op.drop_index(
        "ix_explicacoes_divergencia_empresa_id_criado_em", table_name="explicacoes_divergencia"
    )
    op.drop_index(
        op.f("ix_explicacoes_divergencia_empresa_id"), table_name="explicacoes_divergencia"
    )
    op.drop_table("explicacoes_divergencia")
