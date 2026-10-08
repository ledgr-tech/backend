"""tolerancia_dias_default passa de 2 pra 0 (ADR-008)

Revision ID: 8c1d5e7a9b34
Revises: 2a6441dd07d0
Create Date: 2026-09-21 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c1d5e7a9b34"
down_revision: str | None = "2a6441dd07d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("configuracoes", "tolerancia_dias_default", server_default="0")
    # Backfill: seguro porque nenhuma empresa cliente customiza esse campo ainda.
    op.execute(
        sa.text(
            "UPDATE configuracoes SET tolerancia_dias_default = 0 WHERE tolerancia_dias_default = 2"
        )
    )


def downgrade() -> None:
    # Só o default volta; os valores já gravados não são reescritos, porque
    # não dá pra distinguir um 0 do backfill de um 0 configurado depois.
    op.alter_column("configuracoes", "tolerancia_dias_default", server_default="2")
