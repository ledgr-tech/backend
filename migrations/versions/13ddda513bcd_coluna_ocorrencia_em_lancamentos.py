"""coluna ocorrencia em lancamentos

Revision ID: 13ddda513bcd
Revises: cbe4497d857c
Create Date: 2026-09-21 13:16:27.275378

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "13ddda513bcd"
down_revision: str | None = "cbe4497d857c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default "1": linhas já gravadas viram 1ª ocorrência, e o hash
    # delas continua válido (a fórmula só muda pra ocorrencia > 1, ADR-006).
    op.add_column(
        "lancamentos",
        sa.Column("ocorrencia", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("lancamentos", "ocorrencia")
