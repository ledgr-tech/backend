"""importação do relatório de lançamentos via IA (issue #158, ADR-013)

- `extratos.formato` passa de `varchar(3)` a `varchar(4)` e aceita `json`.
- `extratos.status` aceita `rascunho`: a importação via IA entra assim e fica
  fora de toda conciliação, listagem e contagem até a confirmação (#159).
- `lancamentos.posicao`: a ordem do lançamento no JSON importado, nula nos
  extratos OFX e CSV.
- Índice parcial `ix_extratos_rascunho_criado_em` para a limpeza dos
  rascunhos vencidos.

O downgrade recusa se houver extrato `json`: o esquema anterior não o
comporta, e apagar perderia lançamento de conciliação.

Revision ID: f1c6a8e3b507
Revises: e7a3c5b9d214
Create Date: 2026-10-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c6a8e3b507"
down_revision: str | None = "e7a3c5b9d214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_ANTIGOS = "'pendente', 'processando', 'concluido', 'concluido_com_erros', 'erro'"


def upgrade() -> None:
    op.alter_column(
        "extratos",
        "formato",
        existing_type=sa.String(length=3),
        type_=sa.String(length=4),
        existing_nullable=False,
    )
    op.drop_constraint("ck_extratos_formato_suportado", "extratos", type_="check")
    op.create_check_constraint(
        "ck_extratos_formato_suportado", "extratos", "formato IN ('ofx', 'csv', 'json')"
    )
    op.drop_constraint("ck_extratos_status_valido", "extratos", type_="check")
    op.create_check_constraint(
        "ck_extratos_status_valido", "extratos", f"status IN ({STATUS_ANTIGOS}, 'rascunho')"
    )
    op.create_index(
        "ix_extratos_rascunho_criado_em",
        "extratos",
        ["criado_em"],
        postgresql_where=sa.text("status = 'rascunho'"),
    )
    op.add_column("lancamentos", sa.Column("posicao", sa.Integer(), nullable=True))


def downgrade() -> None:
    restantes = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM extratos WHERE formato = 'json'"))
        .scalar_one()
    )
    if restantes:
        raise RuntimeError(
            f"Downgrade impossível: {restantes} extrato(s) importado(s) via IA depois da "
            "issue #158 não cabem no esquema anterior."
        )
    op.drop_column("lancamentos", "posicao")
    op.drop_index("ix_extratos_rascunho_criado_em", table_name="extratos")
    op.drop_constraint("ck_extratos_status_valido", "extratos", type_="check")
    op.create_check_constraint(
        "ck_extratos_status_valido", "extratos", f"status IN ({STATUS_ANTIGOS})"
    )
    op.drop_constraint("ck_extratos_formato_suportado", "extratos", type_="check")
    op.create_check_constraint(
        "ck_extratos_formato_suportado", "extratos", "formato IN ('ofx', 'csv')"
    )
    op.alter_column(
        "extratos",
        "formato",
        existing_type=sa.String(length=4),
        type_=sa.String(length=3),
        existing_nullable=False,
    )
