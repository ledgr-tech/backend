"""consumo das empresas apagadas, por conta e mês (issue #215)

Tabela nova `consumo_empresas_apagadas`: a limpeza das empresas removidas há
mais de 30 dias apaga as execuções de conciliação delas, e a franquia do mês e
o uso do `/admin` são contados nessas execuções. A limpeza guarda aqui, antes
de apagar, quantos extratos do banco tiveram a primeira execução em cada mês
(`extratos_do_banco`) e quantas execuções houve (`execucoes`), por conta e mês
civil de São Paulo (`mes`, `AAAA-MM`). PK `(conta_id, mes)`: duas empresas da
mesma conta somam na mesma linha. Nasce vazia, sem backfill: nenhuma empresa
foi apagada antes desta revisão, que vem junto com a remoção.

Downgrade: apaga a tabela, e os números das empresas já apagadas saem da
franquia e do `/admin`.

Revision ID: d3f7a9c2e518
Revises: c6a2f8e4b917
Create Date: 2026-11-21 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3f7a9c2e518"
down_revision: str | None = "c6a2f8e4b917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABELA = "consumo_empresas_apagadas"
CHECK = "ck_consumo_empresas_apagadas_positivo"


def upgrade() -> None:
    op.create_table(
        TABELA,
        sa.Column("conta_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mes", sa.String(length=7), nullable=False),
        sa.Column("extratos_do_banco", sa.Integer(), nullable=False),
        sa.Column("execucoes", sa.Integer(), nullable=False),
        sa.CheckConstraint("extratos_do_banco >= 0 AND execucoes >= 0", name=CHECK),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.PrimaryKeyConstraint("conta_id", "mes"),
    )


def downgrade() -> None:
    op.drop_table(TABELA)
