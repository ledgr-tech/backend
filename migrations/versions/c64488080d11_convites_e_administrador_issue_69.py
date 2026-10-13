"""convites, administrador e remoção de usuário (issue #69, ADR-015)

Revision ID: c64488080d11
Revises: 65d4b7d91481
Create Date: 2026-10-13 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c64488080d11"
down_revision: str | None = "65d4b7d91481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_METODO_AUTH = "ck_usuarios_tem_metodo_auth"
CHECK_METODO_AUTH_ANTIGO = "senha_hash IS NOT NULL OR google_sub IS NOT NULL"
# Usuário ativo tem pelo menos um método de login; o removido não tem nenhum.
CHECK_METODO_AUTH_NOVO = (
    "(removido_em IS NULL AND (senha_hash IS NOT NULL OR google_sub IS NOT NULL)) "
    "OR (removido_em IS NOT NULL AND senha_hash IS NULL AND google_sub IS NULL)"
)
UNICO_ADMINISTRADOR = "uq_usuarios_empresa_id_administrador"


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column("administrador", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("usuarios", sa.Column("removido_em", sa.DateTime(timezone=True), nullable=True))

    # Quem cadastrou a empresa é o usuário mais antigo dela. Empate em
    # `criado_em` (mesma transação) desempata pelo id, para sair um só.
    op.execute("""
        UPDATE usuarios SET administrador = true
        WHERE id IN (
            SELECT DISTINCT ON (empresa_id) id
            FROM usuarios
            ORDER BY empresa_id, criado_em, id
        )
        """)
    op.create_index(
        UNICO_ADMINISTRADOR,
        "usuarios",
        ["empresa_id"],
        unique=True,
        postgresql_where=sa.text("administrador"),
    )

    op.drop_constraint(CHECK_METODO_AUTH, "usuarios", type_="check")
    op.create_check_constraint(CHECK_METODO_AUTH, "usuarios", CHECK_METODO_AUTH_NOVO)

    op.create_table(
        "convites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("empresa_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("convidado_por_id", sa.UUID(), nullable=True),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "usado_em IS NULL OR cancelado_em IS NULL",
            name="ck_convites_usado_ou_cancelado",
        ),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.ForeignKeyConstraint(["convidado_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_convites_empresa_id_criado_em",
        "convites",
        ["empresa_id", "criado_em"],
        unique=False,
    )


def downgrade() -> None:
    # Usuário removido não tem método de login e não pode ser apagado (as
    # decisões de linha apontam para ele), então não cabe no CHECK antigo.
    removidos = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM usuarios WHERE removido_em IS NOT NULL"))
        .scalar_one()
    )
    if removidos:
        raise RuntimeError(
            f"Downgrade impossível: {removidos} usuário(s) removido(s) não cabem no CHECK "
            "antigo de usuarios (sem senha e sem google_sub)."
        )

    op.drop_index("ix_convites_empresa_id_criado_em", table_name="convites")
    op.drop_table("convites")
    op.drop_constraint(CHECK_METODO_AUTH, "usuarios", type_="check")
    op.create_check_constraint(CHECK_METODO_AUTH, "usuarios", CHECK_METODO_AUTH_ANTIGO)
    op.drop_index(UNICO_ADMINISTRADOR, table_name="usuarios")
    op.drop_column("usuarios", "removido_em")
    op.drop_column("usuarios", "administrador")
