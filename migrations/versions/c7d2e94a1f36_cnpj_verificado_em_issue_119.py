"""cnpj_verificado_em em empresas (issue #119)

Quando o cadastro confirmou na Receita que o CNPJ existe e está ativo. Nula,
sem backfill: as empresas que já existem ficam sem verificação, porque nenhuma
foi consultada. Nada mais da consulta é guardado.

Revision ID: c7d2e94a1f36
Revises: b41c7e2f9a58
Create Date: 2026-10-16 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d2e94a1f36"
down_revision: str | None = "b41c7e2f9a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "empresas",
        sa.Column("cnpj_verificado_em", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("empresas", "cnpj_verificado_em")
