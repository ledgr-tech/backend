"""chaves da API pública (issue #135, ADR-021)

`chaves_api`: a conta, o escopo (`empresa_id` nulo é a conta inteira), o nome,
o prefixo para reconhecer a chave, o sha256 do token (único), quem criou e
revogou, o último uso e a revogação. O token nunca é gravado.

Revision ID: d7f1b3c5e846
Revises: c3e9a1d5f724
Create Date: 2026-10-22 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7f1b3c5e846"
down_revision: str | None = "c3e9a1d5f724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chaves_api",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=True),
        sa.Column("nome", sa.String(length=60), nullable=False),
        sa.Column("prefixo", sa.String(length=8), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("criada_por_id", sa.UUID(), nullable=True),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ultimo_uso_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revogada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revogada_por_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.ForeignKeyConstraint(["criada_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revogada_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index("ix_chaves_api_conta_id_criado_em", "chaves_api", ["conta_id", "criado_em"])


def downgrade() -> None:
    op.drop_index("ix_chaves_api_conta_id_criado_em", table_name="chaves_api")
    op.drop_table("chaves_api")
