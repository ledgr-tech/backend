"""explicacao_chave em conciliacoes (issue #140, ADR-011)

A chave, em `explicacoes_divergencia`, da explicação da IA de cada linha:
preenchida pela geração automática ao conciliar e pelo `POST /explicacoes`.
Nula, sem backfill: as linhas existentes ganham a ligação quando a explicação
for pedida ou o par for conciliado de novo. Sem índice: o `GET
/conciliacoes/{id}` faz o join a partir da linha, e o lado do cache já tem o
`UNIQUE (empresa_id, chave)`.

Revision ID: b8d4f2a6c391
Revises: a6c2e8f4b157
Create Date: 2026-10-21 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d4f2a6c391"
down_revision: str | None = "a6c2e8f4b157"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conciliacoes", sa.Column("explicacao_chave", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conciliacoes", "explicacao_chave")
