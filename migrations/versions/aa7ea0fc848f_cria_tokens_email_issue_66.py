"""cria tokens_email (issue #66, ADR-012)

Revision ID: aa7ea0fc848f
Revises: dcfc210c931a
Create Date: 2026-09-27 21:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa7ea0fc848f"
down_revision: str | None = "dcfc210c931a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tokens_email",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("finalidade", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "finalidade IN ('recuperacao_senha', 'troca_email')",
            name="ck_tokens_email_finalidade",
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_tokens_email_usuario_id_finalidade_criado_em",
        "tokens_email",
        ["usuario_id", "finalidade", "criado_em"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tokens_email_usuario_id_finalidade_criado_em", table_name="tokens_email")
    op.drop_table("tokens_email")
