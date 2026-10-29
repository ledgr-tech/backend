"""contas, empresas clientes e papéis (issue #133, ADR-019)

O tenant passa a ser a conta. Cada empresa que já existe vira uma conta com
O MESMO id, a mesma razão social, o mesmo CNPJ, a mesma verificação na Receita
e o plano, o teste e a assinatura que estavam na empresa; a empresa fica como
a primeira (e única) empresa cliente dessa conta, com `conta_id` igual ao
próprio id. Reusar o id é o que mantém válidos os JWT já emitidos: o claim
`empresa_id` deles passa a ser lido como o id da conta.

- `empresas`: ganha `conta_id`; o CNPJ deixa de ser único no sistema e passa a
  ser único por conta; plano, teste e assinatura saem para `contas`.
- `usuarios`: `empresa_id` vira `conta_id`; `administrador` vira `papel`
  ('admin', 'gestor' ou 'analista'). O administrador vira 'admin' e os demais,
  removidos inclusive, 'gestor' (mantêm o que podiam fazer). O índice único
  parcial passa a ser `(conta_id) WHERE papel = 'admin'`.
- `convites`: `empresa_id` vira `conta_id`.

As demais tabelas continuam por `empresa_id`, que agora é a empresa cliente.

O downgrade recusa quando alguma conta não tem exatamente uma empresa ou
quando duas empresas têm o mesmo CNPJ (o esquema anterior não comporta
nenhum dos dois); fora isso, desfaz, apontando usuários e convites para a
empresa da conta. O administrador volta a ser quem tem o papel 'admin'.

Revision ID: e3a7c5d9b812
Revises: d5a8f1c3e690
Create Date: 2026-10-21 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a7c5d9b812"
down_revision: str | None = "d5a8f1c3e690"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_PLANO = "plano IN ('essencial', 'padrao', 'avancado')"
CHECK_PLANO_E_FIM = "(plano IS NULL) = (teste_termina_em IS NULL)"
UNICO_ADMINISTRADOR = "uq_usuarios_empresa_id_administrador"
UNICO_ADMIN = "uq_usuarios_conta_id_admin"


def _coluna_conta_id(tabela: str, origem: str) -> None:
    """`conta_id` NOT NULL, com FK para `contas`, copiado de `origem`."""
    op.add_column(tabela, sa.Column("conta_id", sa.UUID(), nullable=True))
    op.execute(f"UPDATE {tabela} SET conta_id = {origem}")
    op.alter_column(tabela, "conta_id", nullable=False)
    op.create_foreign_key(f"{tabela}_conta_id_fkey", tabela, "contas", ["conta_id"], ["id"])


def upgrade() -> None:
    op.create_table(
        "contas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("razao_social", sa.String(), nullable=False),
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("cnpj_verificado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plano", sa.String(), nullable=True),
        sa.Column("teste_termina_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assinatura_ativa", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(CHECK_PLANO, name="ck_contas_plano_valido"),
        sa.CheckConstraint(CHECK_PLANO_E_FIM, name="ck_contas_plano_e_fim_do_teste_juntos"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cnpj"),
    )
    op.execute("""
        INSERT INTO contas (
            id, razao_social, cnpj, cnpj_verificado_em, plano, teste_termina_em,
            assinatura_ativa, criado_em, atualizado_em
        )
        SELECT
            id, razao_social, cnpj, cnpj_verificado_em, plano, teste_termina_em,
            assinatura_ativa, criado_em, atualizado_em
        FROM empresas
        """)

    _coluna_conta_id("empresas", "id")
    op.create_index("ix_empresas_conta_id", "empresas", ["conta_id"], unique=False)
    op.drop_constraint("empresas_cnpj_key", "empresas", type_="unique")
    op.create_unique_constraint("uq_empresas_conta_id_cnpj", "empresas", ["conta_id", "cnpj"])
    op.drop_constraint("ck_empresas_plano_e_fim_do_teste_juntos", "empresas", type_="check")
    op.drop_constraint("ck_empresas_plano_valido", "empresas", type_="check")
    op.drop_column("empresas", "assinatura_ativa")
    op.drop_column("empresas", "teste_termina_em")
    op.drop_column("empresas", "plano")

    _coluna_conta_id("usuarios", "empresa_id")
    op.create_index("ix_usuarios_conta_id", "usuarios", ["conta_id"], unique=False)
    op.add_column("usuarios", sa.Column("papel", sa.String(), nullable=True))
    op.execute("UPDATE usuarios SET papel = CASE WHEN administrador THEN 'admin' ELSE 'gestor' END")
    op.alter_column("usuarios", "papel", nullable=False)
    op.create_check_constraint(
        "ck_usuarios_papel_valido", "usuarios", "papel IN ('admin', 'gestor', 'analista')"
    )
    op.drop_index(UNICO_ADMINISTRADOR, table_name="usuarios")
    op.create_index(
        UNICO_ADMIN,
        "usuarios",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("papel = 'admin'"),
    )
    op.drop_index("ix_usuarios_empresa_id", table_name="usuarios")
    op.drop_column("usuarios", "empresa_id")
    op.drop_column("usuarios", "administrador")

    _coluna_conta_id("convites", "empresa_id")
    op.drop_index("ix_convites_empresa_id_criado_em", table_name="convites")
    op.create_index(
        "ix_convites_conta_id_criado_em", "convites", ["conta_id", "criado_em"], unique=False
    )
    op.drop_column("convites", "empresa_id")


def _recusar_se_nao_cabe(bind) -> None:
    fora = bind.execute(sa.text("""
        SELECT count(*) FROM contas c
        WHERE (SELECT count(*) FROM empresas e WHERE e.conta_id = c.id) <> 1
        """)).scalar_one()
    if fora:
        raise RuntimeError(
            f"Downgrade impossível: {fora} conta(s) sem exatamente uma empresa. Antes da "
            "issue #133, cada empresa era a própria conta."
        )
    repetidos = bind.execute(sa.text("""
        SELECT count(*) FROM (
            SELECT cnpj FROM empresas GROUP BY cnpj HAVING count(*) > 1
        ) AS repetidos
        """)).scalar_one()
    if repetidos:
        raise RuntimeError(
            f"Downgrade impossível: {repetidos} CNPJ(s) em mais de uma empresa. Antes da "
            "issue #133, o CNPJ da empresa era único no sistema."
        )


def _coluna_empresa_id(tabela: str) -> None:
    """`empresa_id` NOT NULL, apontando para a única empresa da conta."""
    op.add_column(tabela, sa.Column("empresa_id", sa.UUID(), nullable=True))
    op.execute(
        f"UPDATE {tabela} t SET empresa_id = e.id FROM empresas e WHERE e.conta_id = t.conta_id"
    )
    op.alter_column(tabela, "empresa_id", nullable=False)
    op.create_foreign_key(f"{tabela}_empresa_id_fkey", tabela, "empresas", ["empresa_id"], ["id"])


def downgrade() -> None:
    _recusar_se_nao_cabe(op.get_bind())

    _coluna_empresa_id("convites")
    op.drop_index("ix_convites_conta_id_criado_em", table_name="convites")
    op.create_index(
        "ix_convites_empresa_id_criado_em", "convites", ["empresa_id", "criado_em"], unique=False
    )
    op.drop_column("convites", "conta_id")

    _coluna_empresa_id("usuarios")
    op.create_index("ix_usuarios_empresa_id", "usuarios", ["empresa_id"], unique=False)
    op.add_column(
        "usuarios",
        sa.Column("administrador", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE usuarios SET administrador = (papel = 'admin')")
    op.drop_index(UNICO_ADMIN, table_name="usuarios")
    op.create_index(
        UNICO_ADMINISTRADOR,
        "usuarios",
        ["empresa_id"],
        unique=True,
        postgresql_where=sa.text("administrador"),
    )
    op.drop_constraint("ck_usuarios_papel_valido", "usuarios", type_="check")
    op.drop_column("usuarios", "papel")
    op.drop_index("ix_usuarios_conta_id", table_name="usuarios")
    op.drop_column("usuarios", "conta_id")

    op.add_column("empresas", sa.Column("plano", sa.String(), nullable=True))
    op.add_column(
        "empresas", sa.Column("teste_termina_em", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "empresas",
        sa.Column("assinatura_ativa", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("""
        UPDATE empresas e
        SET plano = c.plano,
            teste_termina_em = c.teste_termina_em,
            assinatura_ativa = c.assinatura_ativa
        FROM contas c
        WHERE c.id = e.conta_id
        """)
    op.create_check_constraint("ck_empresas_plano_valido", "empresas", CHECK_PLANO)
    op.create_check_constraint(
        "ck_empresas_plano_e_fim_do_teste_juntos", "empresas", CHECK_PLANO_E_FIM
    )
    op.drop_constraint("uq_empresas_conta_id_cnpj", "empresas", type_="unique")
    op.create_unique_constraint("empresas_cnpj_key", "empresas", ["cnpj"])
    op.drop_index("ix_empresas_conta_id", table_name="empresas")
    op.drop_column("empresas", "conta_id")

    op.drop_table("contas")
