"""tarifa_bancaria no CHECK de status de conciliacoes (issue #24)

Revision ID: 9f2c6b4d1a77
Revises: 8c1d5e7a9b34
Create Date: 2026-09-22 16:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f2c6b4d1a77"
down_revision: str | None = "8c1d5e7a9b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_conciliacoes_status_valido", "conciliacoes", type_="check")
    op.create_check_constraint(
        "ck_conciliacoes_status_valido",
        "conciliacoes",
        "status IN ('match_exato', 'match_tolerancia', 'divergente_valor', "
        "'divergente_data', 'sem_correspondencia', 'duplicado', 'tarifa_bancaria')",
    )


def downgrade() -> None:
    # Seguro remover sem backfill: nenhuma empresa cliente real gravou
    # 'tarifa_bancaria' ainda (a classificação é implementada nesta mesma
    # issue). Um downgrade com linhas já em 'tarifa_bancaria' falharia o
    # CHECK — mesmo cenário de qualquer outra migração que estreita um CHECK.
    op.drop_constraint("ck_conciliacoes_status_valido", "conciliacoes", type_="check")
    op.create_check_constraint(
        "ck_conciliacoes_status_valido",
        "conciliacoes",
        "status IN ('match_exato', 'match_tolerancia', 'divergente_valor', "
        "'divergente_data', 'sem_correspondencia', 'duplicado')",
    )
