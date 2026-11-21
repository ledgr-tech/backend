"""remoção de empresa cliente, com 30 dias para restaurar (issue #215)

Colunas novas em `empresas`: `removida_em`, `removida_por_id` (FK para
`usuarios`, `ON DELETE SET NULL`, como o autor dos `fechamentos`),
`removida_por_nome` (cópia do nome no momento) e `apagar_em` (`removida_em`
mais 30 dias). Todas nulas é empresa ativa; as empresas que já existem ficam
ativas, sem backfill.

CHECK `ck_empresas_remocao_completa`: ou os quatro campos de remoção estão
nulos, ou `removida_em`, `apagar_em` e `removida_por_nome` vêm juntos e
`apagar_em` é depois de `removida_em`. `removida_por_id` pode ser nulo na
empresa removida, porque o usuário que removeu pode ser apagado depois.

Índice parcial `ix_empresas_apagar_em WHERE apagar_em IS NOT NULL`, para a
limpeza diária achar as vencidas sem varrer as ativas, que são todas as linhas
de hoje: nasce vazio.

Downgrade: apagar as colunas faz as empresas removidas voltarem a aparecer
como ativas. Por isso o downgrade recusa se houver empresa removida (restaure
ou apague antes).

Revision ID: c6a2f8e4b917
Revises: f2c8d4a7b163
Create Date: 2026-11-20 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c6a2f8e4b917"
down_revision: str | None = "f2c8d4a7b163"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_empresas_remocao_completa"
FK = "fk_empresas_removida_por_id_usuarios"
INDICE = "ix_empresas_apagar_em"


def upgrade() -> None:
    op.add_column("empresas", sa.Column("removida_em", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "empresas", sa.Column("removida_por_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("empresas", sa.Column("removida_por_nome", sa.String(), nullable=True))
    op.add_column("empresas", sa.Column("apagar_em", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        FK, "empresas", "usuarios", ["removida_por_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        CHECK,
        "empresas",
        "(removida_em IS NULL AND apagar_em IS NULL "
        "AND removida_por_id IS NULL AND removida_por_nome IS NULL) "
        "OR (removida_em IS NOT NULL AND apagar_em IS NOT NULL "
        "AND removida_por_nome IS NOT NULL AND apagar_em > removida_em)",
    )
    op.create_index(
        INDICE, "empresas", ["apagar_em"], postgresql_where=sa.text("apagar_em IS NOT NULL")
    )


def downgrade() -> None:
    removidas = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM empresas WHERE removida_em IS NOT NULL")
    )
    if removidas:
        raise RuntimeError(
            f"Há {removidas} empresa(s) removida(s). Restaure ou apague antes do downgrade: "
            "sem as colunas, elas voltariam a aparecer como ativas."
        )
    op.drop_index(INDICE, table_name="empresas")
    op.drop_constraint(CHECK, "empresas", type_="check")
    op.drop_constraint(FK, "empresas", type_="foreignkey")
    op.drop_column("empresas", "apagar_em")
    op.drop_column("empresas", "removida_por_nome")
    op.drop_column("empresas", "removida_por_id")
    op.drop_column("empresas", "removida_em")
