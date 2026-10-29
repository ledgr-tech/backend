"""quem confirmou a importação via IA e quando (issue #159, ADR-013)

`extratos` ganha `confirmado_por_id` (FK para `usuarios`, `ON DELETE SET
NULL`), `confirmado_por_nome` (a cópia do nome no momento, como em
`fechamentos`) e `confirmado_em` (`timestamptz`). Só a confirmação da
importação via IA as preenche; nos outros extratos ficam nulas. O CHECK
exige o nome e a hora juntos, e nenhum dos três sem a hora.

Revision ID: a8d2f6c4e913
Revises: f1c6a8e3b507
Create Date: 2026-10-28 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a8d2f6c4e913"
down_revision: str | None = "f1c6a8e3b507"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extratos",
        sa.Column("confirmado_por_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("extratos", sa.Column("confirmado_por_nome", sa.String(), nullable=True))
    op.add_column("extratos", sa.Column("confirmado_em", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_extratos_confirmado_por_id_usuarios",
        "extratos",
        "usuarios",
        ["confirmado_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_extratos_confirmacao_consistente",
        "extratos",
        "(confirmado_em IS NULL AND confirmado_por_nome IS NULL AND confirmado_por_id IS NULL) "
        "OR (confirmado_em IS NOT NULL AND confirmado_por_nome IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_extratos_confirmacao_consistente", "extratos", type_="check")
    op.drop_constraint("fk_extratos_confirmado_por_id_usuarios", "extratos", type_="foreignkey")
    op.drop_column("extratos", "confirmado_em")
    op.drop_column("extratos", "confirmado_por_nome")
    op.drop_column("extratos", "confirmado_por_id")
