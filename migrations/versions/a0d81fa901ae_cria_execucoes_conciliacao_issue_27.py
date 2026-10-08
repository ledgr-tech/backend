"""cria execucoes_conciliacao (issue #27, ADR-010)

Revision ID: a0d81fa901ae
Revises: 9f2c6b4d1a77
Create Date: 2026-09-23 16:32:32.363840

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0d81fa901ae"
down_revision: str | None = "9f2c6b4d1a77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execucoes_conciliacao",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("extrato_banco_id", sa.UUID(), nullable=False),
        sa.Column("extrato_sistema_id", sa.UUID(), nullable=False),
        sa.Column("tolerancia_dias", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("match_exato", sa.Integer(), nullable=False),
        sa.Column("match_tolerancia", sa.Integer(), nullable=False),
        sa.Column("duplicado", sa.Integer(), nullable=False),
        sa.Column("sem_correspondencia", sa.Integer(), nullable=False),
        sa.Column("tarifa_bancaria", sa.Integer(), nullable=False),
        sa.Column("divergente_valor", sa.Integer(), nullable=False),
        sa.Column("divergente_data", sa.Integer(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "total >= 0 AND match_exato >= 0 AND match_tolerancia >= 0 AND duplicado >= 0 "
            "AND sem_correspondencia >= 0 AND tarifa_bancaria >= 0 AND divergente_valor >= 0 "
            "AND divergente_data >= 0",
            name="ck_execucoes_conciliacao_contagens_nao_negativas",
        ),
        sa.CheckConstraint(
            "total = match_exato + match_tolerancia + duplicado + sem_correspondencia "
            "+ tarifa_bancaria + divergente_valor + divergente_data",
            name="ck_execucoes_conciliacao_total_e_soma_das_categorias",
        ),
        sa.ForeignKeyConstraint(
            ["empresa_id"],
            ["empresas.id"],
        ),
        sa.ForeignKeyConstraint(
            ["extrato_banco_id"],
            ["extratos.id"],
        ),
        sa.ForeignKeyConstraint(
            ["extrato_sistema_id"],
            ["extratos.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_execucoes_conciliacao_empresa_id"),
        "execucoes_conciliacao",
        ["empresa_id"],
        unique=False,
    )
    op.create_index(
        "ix_execucoes_conciliacao_empresa_id_criado_em",
        "execucoes_conciliacao",
        ["empresa_id", "criado_em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execucoes_conciliacao_empresa_id_criado_em", table_name="execucoes_conciliacao"
    )
    op.drop_index(op.f("ix_execucoes_conciliacao_empresa_id"), table_name="execucoes_conciliacao")
    op.drop_table("execucoes_conciliacao")
