"""periodo do extrato e indice por extrato banco em execucoes (issue #82)

Revision ID: 955d370e5332
Revises: aa7ea0fc848f
Create Date: 2026-10-05 12:59:06.930083

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "955d370e5332"
down_revision: str | None = "aa7ea0fc848f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extratos", sa.Column("periodo_inicio", sa.Date(), nullable=True))
    op.add_column("extratos", sa.Column("periodo_fim", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_extratos_periodo_consistente",
        "extratos",
        "(periodo_inicio IS NULL AND periodo_fim IS NULL) "
        "OR (periodo_inicio IS NOT NULL AND periodo_fim IS NOT NULL "
        "AND periodo_inicio <= periodo_fim)",
    )
    op.create_index(
        "ix_execucoes_conciliacao_empresa_id_extrato_banco_id_criado_em",
        "execucoes_conciliacao",
        ["empresa_id", "extrato_banco_id", "criado_em"],
        unique=False,
    )

    # Backfill (issue #82): período de cada extrato já existente, a partir
    # dos lançamentos já gravados. Extrato sem lançamento (upload que nunca
    # terminou de processar, ou terminou em "erro" sem nenhuma linha válida)
    # fica com NULL, igual ao que a normalização passa a gravar dali em diante.
    op.execute(
        sa.text(
            "UPDATE extratos SET periodo_inicio = s.menor, periodo_fim = s.maior "
            "FROM (SELECT extrato_id, MIN(data) AS menor, MAX(data) AS maior "
            "FROM lancamentos GROUP BY extrato_id) s "
            "WHERE extratos.id = s.extrato_id"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execucoes_conciliacao_empresa_id_extrato_banco_id_criado_em",
        table_name="execucoes_conciliacao",
    )
    op.drop_constraint("ck_extratos_periodo_consistente", "extratos", type_="check")
    op.drop_column("extratos", "periodo_fim")
    op.drop_column("extratos", "periodo_inicio")
