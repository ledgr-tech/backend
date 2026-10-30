"""cobrança pela Stripe e teste de 14 dias no cadastro (issues #166 e #167, ADR-026)

- `contas`: sai o CHECK `ck_contas_plano_e_fim_do_teste_juntos` (o teste agora
  existe sem plano) e entra `stripe_customer_id` (único, nulo).
- `assinaturas_stripe`: a assinatura da conta (uma por conta), mantida só pelo
  webhook.
- `eventos_stripe`: id, tipo e datas dos eventos do webhook, para idempotência
  e auditoria. Nunca o payload.
- `pagamentos_stripe`: uma linha por fatura, base da issue #169.
- Dados: a conta sem plano e sem teste ganha `teste_termina_em` = agora + 14
  dias. Contas com plano e teste ficam como estão.

Downgrade: volta o CHECK, o que exige plano e fim do teste juntos. A conta com
teste e sem plano (as criadas depois desta migração) perde o fim do teste e
volta a "sem plano"; a conta com plano e sem teste ganha o fim do teste agora,
ou seja, teste encerrado.

Revision ID: d7a4c2e9b158
Revises: c5f9e2b7a416
Create Date: 2026-10-30 10:12:41.227913

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a4c2e9b158"
down_revision: str | None = "c5f9e2b7a416"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_contas_plano_e_fim_do_teste_juntos", "contas", type_="check")
    op.add_column("contas", sa.Column("stripe_customer_id", sa.String(), nullable=True))
    op.create_unique_constraint("uq_contas_stripe_customer_id", "contas", ["stripe_customer_id"])

    op.create_table(
        "assinaturas_stripe",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("plano", sa.String(), nullable=False),
        sa.Column("periodo", sa.String(), nullable=False),
        sa.Column("periodo_atual_fim", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancelar_no_fim_do_periodo",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("cancelada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "atualizada_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "plano IN ('essencial', 'padrao', 'avancado')",
            name="ck_assinaturas_stripe_plano_valido",
        ),
        sa.CheckConstraint(
            "periodo IN ('mensal', 'trimestral', 'semestral', 'anual')",
            name="ck_assinaturas_stripe_periodo_valido",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conta_id", name="uq_assinaturas_stripe_conta_id"),
        sa.UniqueConstraint("stripe_subscription_id", name="uq_assinaturas_stripe_subscription_id"),
    )
    op.create_table(
        "eventos_stripe",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("processado_em", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pagamentos_stripe",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(), nullable=False),
        sa.Column("conta_id", sa.UUID(), nullable=False),
        sa.Column("valor_centavos", sa.BigInteger(), nullable=False),
        sa.Column("moeda", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("periodo_inicio", sa.DateTime(timezone=True), nullable=True),
        sa.Column("periodo_fim", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ocorrido_em", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('paga', 'falhou')", name="ck_pagamentos_stripe_status"),
        sa.ForeignKeyConstraint(["conta_id"], ["contas.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_invoice_id", name="uq_pagamentos_stripe_invoice_id"),
    )
    op.create_index(
        "ix_pagamentos_stripe_conta_id_ocorrido_em",
        "pagamentos_stripe",
        ["conta_id", "ocorrido_em"],
        unique=False,
    )

    # O cadastro passa a começar o teste: as contas que ainda não escolheram
    # plano (e por isso não têm teste) ganham os 14 dias a partir de agora.
    op.execute(
        "UPDATE contas SET teste_termina_em = now() + interval '14 days' "
        "WHERE plano IS NULL AND teste_termina_em IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_pagamentos_stripe_conta_id_ocorrido_em", table_name="pagamentos_stripe")
    op.drop_table("pagamentos_stripe")
    op.drop_table("eventos_stripe")
    op.drop_table("assinaturas_stripe")

    # O CHECK antigo exige plano e fim do teste juntos.
    op.execute("UPDATE contas SET teste_termina_em = NULL WHERE plano IS NULL")
    op.execute(
        "UPDATE contas SET teste_termina_em = now() WHERE plano IS NOT NULL AND teste_termina_em IS NULL"
    )
    op.drop_constraint("uq_contas_stripe_customer_id", "contas", type_="unique")
    op.drop_column("contas", "stripe_customer_id")
    op.create_check_constraint(
        "ck_contas_plano_e_fim_do_teste_juntos",
        "contas",
        "(plano IS NULL) = (teste_termina_em IS NULL)",
    )
