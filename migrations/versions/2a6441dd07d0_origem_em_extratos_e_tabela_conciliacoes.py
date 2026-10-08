"""origem em extratos e tabela conciliacoes

Revision ID: 2a6441dd07d0
Revises: 13ddda513bcd
Create Date: 2026-09-21 13:47:14.574715

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a6441dd07d0"
down_revision: str | None = "13ddda513bcd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conciliacoes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("extrato_banco_id", sa.UUID(), nullable=False),
        sa.Column("extrato_sistema_id", sa.UUID(), nullable=False),
        sa.Column("lancamento_banco_id", sa.UUID(), nullable=True),
        sa.Column("lancamento_sistema_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("regra_aplicada", sa.String(), nullable=True),
        sa.Column("score_confianca", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "regra_aplicada IS NULL OR regra_aplicada IN ('exato', 'tolerancia', 'fuzzy_descricao')",
            name="ck_conciliacoes_regra_aplicada_valida",
        ),
        sa.CheckConstraint(
            "status IN ('match_exato', 'match_tolerancia', 'divergente_valor', 'divergente_data', 'sem_correspondencia', 'duplicado')",
            name="ck_conciliacoes_status_valido",
        ),
        sa.CheckConstraint(
            "lancamento_banco_id IS NOT NULL OR lancamento_sistema_id IS NOT NULL",
            name="ck_conciliacoes_ao_menos_um_lancamento",
        ),
        sa.CheckConstraint(
            "score_confianca IS NULL OR score_confianca BETWEEN 0 AND 1",
            name="ck_conciliacoes_score_confianca_entre_0_e_1",
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
        sa.ForeignKeyConstraint(
            ["lancamento_banco_id"],
            ["lancamentos.id"],
        ),
        sa.ForeignKeyConstraint(
            ["lancamento_sistema_id"],
            ["lancamentos.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conciliacoes_empresa_id"), "conciliacoes", ["empresa_id"], unique=False
    )
    op.create_index(
        op.f("ix_conciliacoes_extrato_banco_id"), "conciliacoes", ["extrato_banco_id"], unique=False
    )
    op.create_index(
        op.f("ix_conciliacoes_lancamento_banco_id"),
        "conciliacoes",
        ["lancamento_banco_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conciliacoes_lancamento_sistema_id"),
        "conciliacoes",
        ["lancamento_sistema_id"],
        unique=False,
    )
    # server_default 'banco' só pra preencher as linhas já existentes (extratos
    # antigos de dev viram 'banco'); removido em seguida pra o banco ficar sem
    # default, igual ao model: origem é obrigatória no upload.
    op.add_column(
        "extratos",
        sa.Column("origem", sa.String(), server_default="banco", nullable=False),
    )
    op.alter_column("extratos", "origem", server_default=None)
    op.create_check_constraint(
        "ck_extratos_origem_valida", "extratos", "origem IN ('banco', 'sistema')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_extratos_origem_valida", "extratos", type_="check")
    op.drop_column("extratos", "origem")
    op.drop_index(op.f("ix_conciliacoes_lancamento_sistema_id"), table_name="conciliacoes")
    op.drop_index(op.f("ix_conciliacoes_lancamento_banco_id"), table_name="conciliacoes")
    op.drop_index(op.f("ix_conciliacoes_extrato_banco_id"), table_name="conciliacoes")
    op.drop_index(op.f("ix_conciliacoes_empresa_id"), table_name="conciliacoes")
    op.drop_table("conciliacoes")
