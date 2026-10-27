"""empresas_favoritas (issue #157)

Favoritos de empresa por usuário, para o seletor de empresas do header. PK
`(usuario_id, empresa_id)` e `criado_em` em `timestamptz`.

`conta_id` entra só para as duas FKs compostas, `(usuario_id, conta_id)` e
`(empresa_id, conta_id)`, que obrigam usuário e empresa a serem da mesma
conta: favorito de empresa de outra conta não chega a existir. Elas pedem um
UNIQUE `(id, conta_id)` em `usuarios` e em `empresas`, redundante com a PK
mas exigido pelo Postgres como alvo de FK. As duas FKs são `ON DELETE
CASCADE`. O índice em `empresa_id` serve o cascade a partir de `empresas`
(a PK começa pelo usuário).

Tabela nova, sem backfill.

Revision ID: d4b8f1e2a637
Revises: c7e2a9d4f136
Create Date: 2026-10-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4b8f1e2a637"
down_revision: str | None = "c7e2a9d4f136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_usuarios_id_conta_id", "usuarios", ["id", "conta_id"])
    op.create_unique_constraint("uq_empresas_id_conta_id", "empresas", ["id", "conta_id"])
    op.create_table(
        "empresas_favoritas",
        sa.Column("usuario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conta_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("usuario_id", "empresa_id", name="pk_empresas_favoritas"),
        sa.ForeignKeyConstraint(
            ["usuario_id", "conta_id"],
            ["usuarios.id", "usuarios.conta_id"],
            name="fk_empresas_favoritas_usuario_da_conta",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["empresa_id", "conta_id"],
            ["empresas.id", "empresas.conta_id"],
            name="fk_empresas_favoritas_empresa_da_conta",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_empresas_favoritas_empresa_id", "empresas_favoritas", ["empresa_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_empresas_favoritas_empresa_id", table_name="empresas_favoritas")
    op.drop_table("empresas_favoritas")
    op.drop_constraint("uq_empresas_id_conta_id", "empresas", type_="unique")
    op.drop_constraint("uq_usuarios_id_conta_id", "usuarios", type_="unique")
