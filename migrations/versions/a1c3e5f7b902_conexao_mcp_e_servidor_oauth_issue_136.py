"""conexão MCP e servidor de autorização OAuth (issue #136, ADR-022)

`clientes_oauth` (registro dinâmico, sem conta), `pedidos_autorizacao_oauth`
(o authorize esperando o consentimento), `conexoes_mcp` (uma empresa, só
leitura), `codigos_oauth` e `tokens_oauth`. Pedido, código e tokens guardam
só o sha256 do valor aleatório.

Revision ID: a1c3e5f7b902
Revises: f9b3d5a7c124
Create Date: 2026-10-22 18:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1c3e5f7b902"
down_revision: str | None = "f9b3d5a7c124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clientes_oauth",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=100), nullable=True),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "pedidos_autorizacao_oauth",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conta_id", sa.UUID(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clientes_oauth.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["contas.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_table(
        "conexoes_mcp",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("nome_cliente", sa.String(length=100), nullable=False),
        sa.Column("escopo", sa.String(), nullable=False),
        sa.Column("criada_por_id", sa.UUID(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("ultimo_uso_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revogada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revogada_por_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clientes_oauth.client_id"],
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["contas.id"],
        ),
        sa.ForeignKeyConstraint(["criada_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["empresa_id"],
            ["empresas.id"],
        ),
        sa.ForeignKeyConstraint(["revogada_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conexoes_mcp_conta_id_criado_em",
        "conexoes_mcp",
        ["conta_id", "criado_em"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conexoes_mcp_criada_por_id"), "conexoes_mcp", ["criada_por_id"], unique=False
    )
    op.create_table(
        "codigos_oauth",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("conexao_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conexao_id"],
            ["conexoes_mcp.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index(
        op.f("ix_codigos_oauth_conexao_id"), "codigos_oauth", ["conexao_id"], unique=False
    )
    op.create_table(
        "tokens_oauth",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conexao_id", sa.UUID(), nullable=False),
        sa.Column("codigo_id", sa.UUID(), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("hash_acesso", sa.String(length=64), nullable=False),
        sa.Column("hash_renovacao", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("acesso_expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renovacao_expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renovado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revogado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["codigo_id"],
            ["codigos_oauth.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conexao_id"],
            ["conexoes_mcp.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash_acesso"),
        sa.UniqueConstraint("hash_renovacao"),
    )
    op.create_index(op.f("ix_tokens_oauth_codigo_id"), "tokens_oauth", ["codigo_id"], unique=False)
    op.create_index(
        op.f("ix_tokens_oauth_conexao_id"), "tokens_oauth", ["conexao_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tokens_oauth_conexao_id"), table_name="tokens_oauth")
    op.drop_index(op.f("ix_tokens_oauth_codigo_id"), table_name="tokens_oauth")
    op.drop_table("tokens_oauth")
    op.drop_index(op.f("ix_codigos_oauth_conexao_id"), table_name="codigos_oauth")
    op.drop_table("codigos_oauth")
    op.drop_index(op.f("ix_conexoes_mcp_criada_por_id"), table_name="conexoes_mcp")
    op.drop_index("ix_conexoes_mcp_conta_id_criado_em", table_name="conexoes_mcp")
    op.drop_table("conexoes_mcp")
    op.drop_table("pedidos_autorizacao_oauth")
    op.drop_table("clientes_oauth")
