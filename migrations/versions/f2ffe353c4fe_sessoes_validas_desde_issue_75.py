"""usuarios.sessoes_validas_desde (issue #75, ADR-016)

Revision ID: f2ffe353c4fe
Revises: c64488080d11
Create Date: 2026-10-13 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2ffe353c4fe"
down_revision: str | None = "c64488080d11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Sem backfill: nulo quer dizer que nenhuma sessão foi encerrada.
    op.add_column(
        "usuarios",
        sa.Column("sessoes_validas_desde", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usuarios", "sessoes_validas_desde")
