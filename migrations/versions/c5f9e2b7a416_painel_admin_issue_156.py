"""painel administrativo interno (issue #156, ADR-025)

- `operadores_ledgr`: o operador do Ledgr, ligado a um usuário, com um ativo
  por usuário (índice único parcial). Só o script scripts/operadores.py
  escreve nela.
- `acessos_admin`: toda requisição ao `/admin` com sessão válida, só de
  inserção e sem FK (sobrevive à exclusão da conta consultada).
- `eventos_conta`: inativação, reativação e piloto ativo, só de inserção.
- `contas.inativada_em` e `motivo_inativacao`, juntos (CHECK), à parte da
  situação comercial.
- `usuarios.ultimo_login_em`.

Revision ID: c5f9e2b7a416
Revises: b3e7d1a9c582
Create Date: 2026-10-28 15:22:33.332538

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f9e2b7a416"
down_revision: str | None = "b3e7d1a9c582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "acessos_admin",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("operador_id", sa.UUID(), nullable=True),
        sa.Column("metodo", sa.String(length=10), nullable=False),
        sa.Column("rota", sa.String(length=200), nullable=False),
        sa.Column("conta_consultada_id", sa.UUID(), nullable=True),
        sa.Column("resultado", sa.String(length=20), nullable=False),
        sa.Column(
            "em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resultado IN ('permitido', 'nao_operador', 'login_antigo')",
            name="ck_acessos_admin_resultado",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_acessos_admin_conta_consultada_id_em",
        "acessos_admin",
        ["conta_consultada_id", "em"],
        unique=False,
    )
    op.create_index("ix_acessos_admin_em", "acessos_admin", ["em"], unique=False)
    op.create_table(
        "operadores_ledgr",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("incluido_por", sa.String(length=100), nullable=False),
        sa.Column(
            "incluido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("removido_por", sa.String(length=100), nullable=True),
        sa.Column("removido_em", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(removido_em IS NULL AND removido_por IS NULL) OR (removido_em IS NOT NULL AND removido_por IS NOT NULL)",
            name="ck_operadores_ledgr_remocao_completa",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_operadores_ledgr_usuario_id_ativo",
        "operadores_ledgr",
        ["usuario_id"],
        unique=True,
        postgresql_where=sa.text("removido_em IS NULL"),
    )
    op.create_table(
        "eventos_conta",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("operador_id", sa.UUID(), nullable=True),
        sa.Column("operador_nome", sa.String(), nullable=True),
        sa.Column("origem", sa.String(length=10), nullable=False),
        sa.Column(
            "em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("origem IN ('painel', 'script')", name="ck_eventos_conta_origem"),
        sa.CheckConstraint(
            "tipo IN ('inativada', 'reativada', 'piloto_ligado', 'piloto_desligado')",
            name="ck_eventos_conta_tipo",
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["contas.id"],
        ),
        sa.ForeignKeyConstraint(
            ["operador_id"],
            ["operadores_ledgr.id"],
            name="fk_eventos_conta_operador_id_operadores_ledgr",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eventos_conta_conta_id_em", "eventos_conta", ["conta_id", "em"], unique=False
    )
    op.add_column("contas", sa.Column("inativada_em", sa.DateTime(timezone=True), nullable=True))
    op.add_column("contas", sa.Column("motivo_inativacao", sa.String(length=500), nullable=True))
    op.add_column(
        "usuarios", sa.Column("ultimo_login_em", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_contas_inativacao_completa",
        "contas",
        "(inativada_em IS NULL) = (motivo_inativacao IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_contas_inativacao_completa", "contas", type_="check")
    op.drop_column("usuarios", "ultimo_login_em")
    op.drop_column("contas", "motivo_inativacao")
    op.drop_column("contas", "inativada_em")
    op.drop_index("ix_eventos_conta_conta_id_em", table_name="eventos_conta")
    op.drop_table("eventos_conta")
    op.drop_index(
        "uq_operadores_ledgr_usuario_id_ativo",
        table_name="operadores_ledgr",
        postgresql_where=sa.text("removido_em IS NULL"),
    )
    op.drop_table("operadores_ledgr")
    op.drop_index("ix_acessos_admin_em", table_name="acessos_admin")
    op.drop_index("ix_acessos_admin_conta_consultada_id_em", table_name="acessos_admin")
    op.drop_table("acessos_admin")
