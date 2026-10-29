"""preferências de primeiro uso do usuário (issue #147)

`usuarios.tutoriais_dispensados` (array de texto, padrão vazio) e
`usuarios.roteiro_dispensado` (boolean, padrão false). Os usuários que já
existem ficam com o padrão.

Revision ID: e8a2c4f6b913
Revises: d7f1b3c5e846
Create Date: 2026-10-22 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8a2c4f6b913"
down_revision: str | None = "d7f1b3c5e846"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column(
            "tutoriais_dispensados",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "usuarios",
        sa.Column(
            "roteiro_dispensado",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("usuarios", "roteiro_dispensado")
    op.drop_column("usuarios", "tutoriais_dispensados")
