"""plano, teste_termina_em e assinatura_ativa em empresas (issue #122)

Plano escolhido (nulo, com CHECK nos três planos), fim do período de teste
(timestamptz nulo, preenchido na primeira escolha) e assinatura ativa
(booleano, falso por padrão). Um segundo CHECK garante que plano e fim do
teste vêm juntos. As empresas que já existem ficam sem plano, com assinatura
inativa: situação "sem_plano". A situação é calculada, não é coluna.

Revision ID: d5a8f1c3e690
Revises: c7d2e94a1f36
Create Date: 2026-10-16 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a8f1c3e690"
down_revision: str | None = "c7d2e94a1f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("empresas", sa.Column("plano", sa.String(), nullable=True))
    op.add_column(
        "empresas", sa.Column("teste_termina_em", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "empresas",
        sa.Column("assinatura_ativa", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "ck_empresas_plano_valido", "empresas", "plano IN ('essencial', 'padrao', 'avancado')"
    )
    op.create_check_constraint(
        "ck_empresas_plano_e_fim_do_teste_juntos",
        "empresas",
        "(plano IS NULL) = (teste_termina_em IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_empresas_plano_e_fim_do_teste_juntos", "empresas", type_="check")
    op.drop_constraint("ck_empresas_plano_valido", "empresas", type_="check")
    op.drop_column("empresas", "assinatura_ativa")
    op.drop_column("empresas", "teste_termina_em")
    op.drop_column("empresas", "plano")
