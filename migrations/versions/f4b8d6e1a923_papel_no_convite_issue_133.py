"""papel no convite (issue #133, parte 2, ADR-019)

`convites.papel`: o papel que o convidado recebe no aceite, 'gestor' ou
'analista' (o admin só muda por transferência). Os convites que já existem
recebem 'gestor', coerente com a migração das contas, que fez os convidados de
antes virarem gestores.

Revision ID: f4b8d6e1a923
Revises: e3a7c5d9b812
Create Date: 2026-10-21 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b8d6e1a923"
down_revision: str | None = "e3a7c5d9b812"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("convites", sa.Column("papel", sa.String(), nullable=True))
    op.execute("UPDATE convites SET papel = 'gestor'")
    op.alter_column("convites", "papel", nullable=False)
    op.create_check_constraint(
        "ck_convites_papel_valido", "convites", "papel IN ('gestor', 'analista')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_convites_papel_valido", "convites", type_="check")
    op.drop_column("convites", "papel")
