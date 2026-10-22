"""uso do assistente e perguntas sem resposta (issue #137, ADR-020)

`usos_assistente`: só a conta e o instante de cada pergunta respondida, para os
limites diários. `perguntas_sem_resposta`: a pergunta já mascarada, a tela e o
instante, sem usuário, conta nem empresa. As duas apagam o que tem mais de 90
dias a cada gravação.

Revision ID: c3e9a1d5f724
Revises: b8d4f2a6c391
Create Date: 2026-10-22 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e9a1d5f724"
down_revision: str | None = "b8d4f2a6c391"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usos_assistente",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usos_assistente_conta_id_criado_em", "usos_assistente", ["conta_id", "criado_em"]
    )
    op.create_index("ix_usos_assistente_criado_em", "usos_assistente", ["criado_em"])
    op.create_table(
        "perguntas_sem_resposta",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pergunta", sa.Text(), nullable=False),
        sa.Column("tela", sa.String(length=64), nullable=True),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_perguntas_sem_resposta_criado_em", "perguntas_sem_resposta", ["criado_em"])


def downgrade() -> None:
    op.drop_index("ix_perguntas_sem_resposta_criado_em", table_name="perguntas_sem_resposta")
    op.drop_table("perguntas_sem_resposta")
    op.drop_index("ix_usos_assistente_criado_em", table_name="usos_assistente")
    op.drop_index("ix_usos_assistente_conta_id_criado_em", table_name="usos_assistente")
    op.drop_table("usos_assistente")
