"""lote_id em decisoes_linha (issue #237)

`decisoes_linha.lote_id` (uuid, nulo): o mesmo valor em todos os eventos
`justificada` gravados por um `POST /conciliacoes/{extrato_id}/decisoes/lote`,
nulo em todo o resto. O CHECK `ck_decisoes_linha_lote_so_na_justificada` só
aceita `lote_id` preenchido quando `tipo = 'justificada'`. Os eventos que já
existem ficam com nulo, sem backfill: nenhum foi feito em lote.

Índice parcial `(empresa_id, extrato_banco_id, lote_id) WHERE lote_id IS NOT
NULL`, para achar as linhas de um lote (a contagem `lote_tamanho` da listagem
e a aprovação por `lote_id`) sem pesar nos eventos fora de lote, que são a
maioria. Na criação a coluna é toda nula, então o índice nasce vazio e o
`CREATE INDEX` não varre nada que valha a pena.

Downgrade: remove o índice, o CHECK e a coluna. Perde-se só a ligação entre
as justificativas do mesmo lote; cada justificativa continua inteira e válida
para o CHECK de texto, então não há o que recusar.

Revision ID: f2c8d4a7b163
Revises: b9e3c7a1d548
Create Date: 2026-11-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2c8d4a7b163"
down_revision: str | None = "b9e3c7a1d548"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDICE = "ix_decisoes_linha_empresa_id_extrato_banco_id_lote_id"
CHECK = "ck_decisoes_linha_lote_so_na_justificada"


def upgrade() -> None:
    op.add_column(
        "decisoes_linha", sa.Column("lote_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_check_constraint(CHECK, "decisoes_linha", "lote_id IS NULL OR tipo = 'justificada'")
    op.create_index(
        INDICE,
        "decisoes_linha",
        ["empresa_id", "extrato_banco_id", "lote_id"],
        postgresql_where=sa.text("lote_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDICE, table_name="decisoes_linha")
    op.drop_constraint(CHECK, "decisoes_linha", type_="check")
    op.drop_column("decisoes_linha", "lote_id")
