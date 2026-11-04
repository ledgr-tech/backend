"""cidade e uf em contas (issue #184)

`contas.cidade` (texto, nulo, de 2 a 80 caracteres) e `contas.uf` (2 letras,
nulo, uma das 27 siglas), com um CHECK cada um. As contas que já existem ficam
com os dois nulos, sem backfill: o `/admin` mostra "não informada". O dado é
da conta, não da empresa cliente.

Sem índice em `contas.uf`: o filtro e a contagem do `/admin` varrem a tabela
de contas, que tem uma linha por cliente do Ledgr (centenas, não milhões), e
um índice numa coluna de 27 valores distintos nem seria usado pelo planner.

Downgrade: remove os CHECKs e as duas colunas (o dado de localização se
perde, mas nenhuma outra tabela depende dele).

Revision ID: b9e3c7a1d548
Revises: a4d7e1b9c352
Create Date: 2026-11-04 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9e3c7a1d548"
down_revision: str | None = "a4d7e1b9c352"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UFS = (
    "AC",
    "AL",
    "AM",
    "AP",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MG",
    "MS",
    "MT",
    "PA",
    "PB",
    "PE",
    "PI",
    "PR",
    "RJ",
    "RN",
    "RO",
    "RR",
    "RS",
    "SC",
    "SE",
    "SP",
    "TO",
)
# Copiada de app/core/localizacao.py de propósito: a migração não importa o app.
UF_VALIDA = "uf IS NULL OR uf IN (" + ", ".join(f"'{uf}'" for uf in UFS) + ")"
CIDADE_TAMANHO = "cidade IS NULL OR char_length(cidade) BETWEEN 2 AND 80"


def upgrade() -> None:
    op.add_column("contas", sa.Column("cidade", sa.String(), nullable=True))
    op.add_column("contas", sa.Column("uf", sa.String(length=2), nullable=True))
    op.create_check_constraint("ck_contas_cidade_tamanho", "contas", CIDADE_TAMANHO)
    op.create_check_constraint("ck_contas_uf_valida", "contas", UF_VALIDA)


def downgrade() -> None:
    op.drop_constraint("ck_contas_uf_valida", "contas", type_="check")
    op.drop_constraint("ck_contas_cidade_tamanho", "contas", type_="check")
    op.drop_column("contas", "uf")
    op.drop_column("contas", "cidade")
