"""painel administrativo, parte 2: cancelamentos e cupons (issue #169, ADR-025 e ADR-026)

- `assinaturas_stripe`: `motivo_cancelamento` (o `cancellation_details.reason`
  da Stripe) e `origem_cancelamento` (cliente, falta_de_pagamento, operador ou
  painel_stripe, com CHECK), gravados a cada sincronização.
- `cupons_enviados`: o cupom que o operador deu a uma conta, na forma de código
  promocional ou aplicado direto na assinatura.
- `eventos_conta`: `tipo` passa de 20 para 30 caracteres (`cancelamento_agendado`
  tem 21) e o CHECK ganha `assinatura_cancelada`, `cancelamento_agendado`,
  `cancelamento_desfeito` e `cupom_enviado`.

Downgrade: apaga os eventos dos tipos novos (o CHECK antigo os recusaria),
devolve `tipo` a 20 caracteres e tira as colunas e a tabela.

Revision ID: e8b3a6d1f294
Revises: d7a4c2e9b158
Create Date: 2026-11-03 18:40:12.518204

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b3a6d1f294"
down_revision: str | None = "d7a4c2e9b158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIPOS_ANTIGOS = "'inativada', 'reativada', 'piloto_ligado', 'piloto_desligado'"
TIPOS_NOVOS = (
    TIPOS_ANTIGOS
    + ", 'assinatura_cancelada', 'cancelamento_agendado', 'cancelamento_desfeito', 'cupom_enviado'"
)


def upgrade() -> None:
    op.add_column(
        "assinaturas_stripe", sa.Column("motivo_cancelamento", sa.String(), nullable=True)
    )
    op.add_column(
        "assinaturas_stripe", sa.Column("origem_cancelamento", sa.String(), nullable=True)
    )
    op.create_check_constraint(
        "ck_assinaturas_stripe_origem_cancelamento",
        "assinaturas_stripe",
        "origem_cancelamento IS NULL OR origem_cancelamento IN "
        "('cliente', 'falta_de_pagamento', 'operador', 'painel_stripe')",
    )

    op.create_table(
        "cupons_enviados",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("operador_id", sa.UUID(), nullable=True),
        sa.Column("operador_nome", sa.String(), nullable=True),
        sa.Column("stripe_coupon_id", sa.String(), nullable=False),
        sa.Column("cupom_nome", sa.String(), nullable=False),
        sa.Column("desconto", sa.String(), nullable=False),
        sa.Column("forma", sa.String(length=10), nullable=False),
        sa.Column("codigo", sa.String(), nullable=True),
        sa.Column("stripe_promotion_code_id", sa.String(), nullable=True),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_enviado", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("usado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("forma IN ('codigo', 'assinatura')", name="ck_cupons_enviados_forma"),
        sa.CheckConstraint(
            "(forma = 'codigo' AND codigo IS NOT NULL AND stripe_promotion_code_id IS NOT NULL "
            "AND expira_em IS NOT NULL) "
            "OR (forma = 'assinatura' AND codigo IS NULL AND stripe_promotion_code_id IS NULL "
            "AND expira_em IS NULL)",
            name="ck_cupons_enviados_forma_e_codigo",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.ForeignKeyConstraint(
            ["operador_id"],
            ["operadores_ledgr.id"],
            name="fk_cupons_enviados_operador_id_operadores_ledgr",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stripe_promotion_code_id", name="uq_cupons_enviados_stripe_promotion_code_id"
        ),
    )
    op.create_index(
        "ix_cupons_enviados_conta_id_em", "cupons_enviados", ["conta_id", "em"], unique=False
    )

    op.alter_column(
        "eventos_conta",
        "tipo",
        existing_type=sa.String(length=20),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
    op.drop_constraint("ck_eventos_conta_tipo", "eventos_conta", type_="check")
    op.create_check_constraint("ck_eventos_conta_tipo", "eventos_conta", f"tipo IN ({TIPOS_NOVOS})")


def downgrade() -> None:
    op.drop_constraint("ck_eventos_conta_tipo", "eventos_conta", type_="check")
    op.execute(f"DELETE FROM eventos_conta WHERE tipo NOT IN ({TIPOS_ANTIGOS})")
    op.alter_column(
        "eventos_conta",
        "tipo",
        existing_type=sa.String(length=30),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_eventos_conta_tipo", "eventos_conta", f"tipo IN ({TIPOS_ANTIGOS})"
    )

    op.drop_index("ix_cupons_enviados_conta_id_em", table_name="cupons_enviados")
    op.drop_table("cupons_enviados")

    op.drop_constraint(
        "ck_assinaturas_stripe_origem_cancelamento", "assinaturas_stripe", type_="check"
    )
    op.drop_column("assinaturas_stripe", "origem_cancelamento")
    op.drop_column("assinaturas_stripe", "motivo_cancelamento")
