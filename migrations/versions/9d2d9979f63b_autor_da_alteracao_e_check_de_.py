"""autor da alteracao e check de tolerancia em configuracoes (issue #85)

Revision ID: 9d2d9979f63b
Revises: 77b5981fda9c
Create Date: 2026-10-06 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d2d9979f63b"
down_revision: str | None = "77b5981fda9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("configuracoes", sa.Column("atualizado_por_id", sa.UUID(), nullable=True))
    op.add_column("configuracoes", sa.Column("atualizado_por_nome", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_configuracoes_atualizado_por_id_usuarios",
        "configuracoes",
        "usuarios",
        ["atualizado_por_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Só o mínimo vai pro banco; o máximo (5 dias) é regra de produto e fica
    # na API (app/api/empresa.py), pra mudar sem migração.
    op.create_check_constraint(
        "ck_configuracoes_tolerancia_dias_nao_negativa",
        "configuracoes",
        "tolerancia_dias_default >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_configuracoes_tolerancia_dias_nao_negativa", "configuracoes", type_="check"
    )
    op.drop_constraint(
        "fk_configuracoes_atualizado_por_id_usuarios", "configuracoes", type_="foreignkey"
    )
    op.drop_column("configuracoes", "atualizado_por_nome")
    op.drop_column("configuracoes", "atualizado_por_id")
