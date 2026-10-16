"""saldo_final e saldo_data em extratos (issue #118)

Saldo que o arquivo do extrato informa (LEDGERBAL do OFX ou a coluna `saldo`
do CSV) e a data dele, os dois nulos e com um CHECK de que vêm juntos.

Sem backfill: os extratos já enviados ficam com as duas colunas nulas. O saldo
só existe no arquivo bruto, que não é guardado (ADR-002), e não dá para
deduzir dos lançamentos gravados. Quem quiser o saldo de um extrato antigo
reenvia o arquivo.

Revision ID: b41c7e2f9a58
Revises: 3e8b5d2a7c41
Create Date: 2026-10-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b41c7e2f9a58"
down_revision: str | None = "3e8b5d2a7c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extratos", sa.Column("saldo_final", sa.Numeric(14, 2), nullable=True))
    op.add_column("extratos", sa.Column("saldo_data", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_extratos_saldo_consistente",
        "extratos",
        "(saldo_final IS NULL AND saldo_data IS NULL) "
        "OR (saldo_final IS NOT NULL AND saldo_data IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_extratos_saldo_consistente", "extratos", type_="check")
    op.drop_column("extratos", "saldo_data")
    op.drop_column("extratos", "saldo_final")
