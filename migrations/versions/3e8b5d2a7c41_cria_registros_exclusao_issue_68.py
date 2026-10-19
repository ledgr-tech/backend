"""cria registros_exclusao (issue #68, ADR-017)

Revision ID: 3e8b5d2a7c41
Revises: f2ffe353c4fe
Create Date: 2026-10-14 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3e8b5d2a7c41"
down_revision: str | None = "f2ffe353c4fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `empresa_id` sem FK de propósito: a empresa do registro já foi apagada.
    op.create_table(
        "registros_exclusao",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("cnpj_sha256", sa.String(length=64), nullable=False),
        sa.Column("contagens", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "executado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("executado_por", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("registros_exclusao")
