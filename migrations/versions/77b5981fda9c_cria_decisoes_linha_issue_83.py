"""cria decisoes_linha (issue #83)

Revision ID: 77b5981fda9c
Revises: 955d370e5332
Create Date: 2026-10-05 15:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "77b5981fda9c"
down_revision: str | None = "955d370e5332"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decisoes_linha",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("extrato_banco_id", sa.UUID(), nullable=False),
        sa.Column("chave", sa.String(length=64), nullable=False),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("texto", sa.String(), nullable=True),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("autor_nome", sa.String(), nullable=False),
        sa.Column("rodada", sa.Integer(), nullable=False),
        sa.Column("extrato_sistema_id", sa.UUID(), nullable=False),
        sa.Column(
            "em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "tipo IN ('conferida', 'conferencia_desfeita', 'justificada', "
            "'justificativa_desfeita')",
            name="ck_decisoes_linha_tipo_valido",
        ),
        sa.CheckConstraint(
            "(tipo = 'justificada' AND texto IS NOT NULL) "
            "OR (tipo <> 'justificada' AND texto IS NULL)",
            name="ck_decisoes_linha_texto_so_na_justificativa",
        ),
        sa.CheckConstraint("rodada >= 1", name="ck_decisoes_linha_rodada_positiva"),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.ForeignKeyConstraint(["extrato_banco_id"], ["extratos.id"]),
        sa.ForeignKeyConstraint(["extrato_sistema_id"], ["extratos.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_decisoes_linha_empresa_id_extrato_banco_id_chave_em",
        "decisoes_linha",
        ["empresa_id", "extrato_banco_id", "chave", "em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decisoes_linha_empresa_id_extrato_banco_id_chave_em", table_name="decisoes_linha"
    )
    op.drop_table("decisoes_linha")
