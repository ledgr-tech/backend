"""arquivo_chave em extratos (issue #139, ADR-023)

A chave do objeto no bucket do R2 quando o arquivo original do extrato foi
guardado (plano avançado, com ARQUIVO_ORIGINAL_HABILITADO ligada). O arquivo
fica cifrado no R2, nunca no banco.

Sem backfill: os extratos já enviados não têm original guardado. O arquivo
bruto foi descartado depois do processamento (ADR-002), e não há de onde tirar.

Revision ID: c7e2a9d4f136
Revises: a1c3e5f7b902
Create Date: 2026-10-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e2a9d4f136"
down_revision: str | None = "a1c3e5f7b902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extratos", sa.Column("arquivo_chave", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("extratos", "arquivo_chave")
