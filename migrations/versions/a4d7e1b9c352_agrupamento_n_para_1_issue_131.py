"""agrupamento N para 1 e 1 para N no matching (issue #131, ADR-027)

- `conciliacoes`: coluna `grupo_id` (uuid, nula, com índice), `match_agrupado`
  no CHECK de status, `agrupamento` no CHECK de regra e dois CHECKs novos:
  `grupo_id` só existe na linha `match_agrupado` (e toda `match_agrupado` o
  tem), e a linha `match_agrupado` tem exatamente uma das duas pontas de
  lançamento.
- `execucoes_conciliacao`: colunas `match_agrupado` (linhas) e `agrupamentos`
  (grupos), inteiras, not null, default 0 (as execuções antigas ficam com 0), e
  os CHECKs de contagens não negativas e da soma das categorias passam a
  incluir `match_agrupado`. `agrupamentos` fica fora da soma.

Downgrade: devolve ao motor antigo o que ele teria feito. `conciliacoes` é
derivada e se refaz ao reconciliar, e a linha `match_agrupado` não existe no
esquema antigo: volta a `sem_correspondencia` (sem regra, score nem grupo), ou
seja, de volta a divergência, e nunca fica conciliada sem o motor que a
conciliou. `execucoes_conciliacao` é histórico só de inserção, e o CHECK antigo
exige que `total` seja a soma das sete categorias: as linhas agrupadas de cada
execução entram em `sem_correspondencia`, a mesma categoria das linhas acima, e
`total` não muda. Os resumos congelados dos fechamentos (JSON) não são
alterados: as chaves novas ficam lá, sem efeito no código antigo.

Revision ID: a4d7e1b9c352
Revises: e8b3a6d1f294
Create Date: 2026-11-04 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4d7e1b9c352"
down_revision: str | None = "e8b3a6d1f294"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_ANTIGOS = (
    "status IN ('match_exato', 'match_tolerancia', 'divergente_valor', "
    "'divergente_data', 'sem_correspondencia', 'duplicado', 'tarifa_bancaria')"
)
STATUS_NOVOS = (
    "status IN ('match_exato', 'match_tolerancia', 'match_agrupado', "
    "'divergente_valor', 'divergente_data', 'sem_correspondencia', 'duplicado', "
    "'tarifa_bancaria')"
)
REGRA_ANTIGA = (
    "regra_aplicada IS NULL OR regra_aplicada IN ('exato', 'tolerancia', 'fuzzy_descricao')"
)
REGRA_NOVA = (
    "regra_aplicada IS NULL OR regra_aplicada IN "
    "('exato', 'tolerancia', 'fuzzy_descricao', 'agrupamento')"
)
GRUPO_SO_NO_AGRUPADO = "(status = 'match_agrupado') = (grupo_id IS NOT NULL)"
AGRUPADO_UMA_PONTA = (
    "status <> 'match_agrupado' OR num_nonnulls(lancamento_banco_id, lancamento_sistema_id) = 1"
)

NAO_NEGATIVAS_ANTIGA = (
    "total >= 0 AND match_exato >= 0 AND match_tolerancia >= 0 AND duplicado >= 0 "
    "AND sem_correspondencia >= 0 AND tarifa_bancaria >= 0 AND divergente_valor >= 0 "
    "AND divergente_data >= 0"
)
NAO_NEGATIVAS_NOVA = NAO_NEGATIVAS_ANTIGA + " AND match_agrupado >= 0 AND agrupamentos >= 0"
SOMA_ANTIGA = (
    "total = match_exato + match_tolerancia + duplicado + sem_correspondencia "
    "+ tarifa_bancaria + divergente_valor + divergente_data"
)
SOMA_NOVA = (
    "total = match_exato + match_tolerancia + match_agrupado + duplicado "
    "+ sem_correspondencia + tarifa_bancaria + divergente_valor + divergente_data"
)


def _trocar_checks_das_execucoes(nao_negativas: str, soma: str) -> None:
    op.drop_constraint(
        "ck_execucoes_conciliacao_contagens_nao_negativas",
        "execucoes_conciliacao",
        type_="check",
    )
    op.drop_constraint(
        "ck_execucoes_conciliacao_total_e_soma_das_categorias",
        "execucoes_conciliacao",
        type_="check",
    )
    op.create_check_constraint(
        "ck_execucoes_conciliacao_contagens_nao_negativas", "execucoes_conciliacao", nao_negativas
    )
    op.create_check_constraint(
        "ck_execucoes_conciliacao_total_e_soma_das_categorias", "execucoes_conciliacao", soma
    )


def upgrade() -> None:
    op.add_column("conciliacoes", sa.Column("grupo_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_conciliacoes_grupo_id"), "conciliacoes", ["grupo_id"], unique=False)
    op.drop_constraint("ck_conciliacoes_status_valido", "conciliacoes", type_="check")
    op.drop_constraint("ck_conciliacoes_regra_aplicada_valida", "conciliacoes", type_="check")
    op.create_check_constraint("ck_conciliacoes_status_valido", "conciliacoes", STATUS_NOVOS)
    op.create_check_constraint("ck_conciliacoes_regra_aplicada_valida", "conciliacoes", REGRA_NOVA)
    op.create_check_constraint(
        "ck_conciliacoes_grupo_id_so_no_match_agrupado", "conciliacoes", GRUPO_SO_NO_AGRUPADO
    )
    op.create_check_constraint(
        "ck_conciliacoes_match_agrupado_uma_ponta", "conciliacoes", AGRUPADO_UMA_PONTA
    )

    # As colunas novas entram com 0, então a soma nova (com `match_agrupado`)
    # vale para as linhas que já existem.
    op.add_column(
        "execucoes_conciliacao",
        sa.Column("match_agrupado", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "execucoes_conciliacao",
        sa.Column("agrupamentos", sa.Integer(), server_default="0", nullable=False),
    )
    _trocar_checks_das_execucoes(NAO_NEGATIVAS_NOVA, SOMA_NOVA)


def downgrade() -> None:
    # Execuções: as linhas agrupadas viram `sem_correspondencia` e o total fica
    # como está (ver a docstring). Os CHECKs novos saem antes da conta.
    op.drop_constraint(
        "ck_execucoes_conciliacao_contagens_nao_negativas",
        "execucoes_conciliacao",
        type_="check",
    )
    op.drop_constraint(
        "ck_execucoes_conciliacao_total_e_soma_das_categorias",
        "execucoes_conciliacao",
        type_="check",
    )
    op.execute(
        "UPDATE execucoes_conciliacao "
        "SET sem_correspondencia = sem_correspondencia + match_agrupado "
        "WHERE match_agrupado > 0"
    )
    op.drop_column("execucoes_conciliacao", "agrupamentos")
    op.drop_column("execucoes_conciliacao", "match_agrupado")
    op.create_check_constraint(
        "ck_execucoes_conciliacao_contagens_nao_negativas",
        "execucoes_conciliacao",
        NAO_NEGATIVAS_ANTIGA,
    )
    op.create_check_constraint(
        "ck_execucoes_conciliacao_total_e_soma_das_categorias", "execucoes_conciliacao", SOMA_ANTIGA
    )

    # Conciliações: as linhas agrupadas voltam a divergência. Os CHECKs novos
    # saem antes, porque `status` e `grupo_id` mudam juntos no UPDATE.
    op.drop_constraint("ck_conciliacoes_match_agrupado_uma_ponta", "conciliacoes", type_="check")
    op.drop_constraint(
        "ck_conciliacoes_grupo_id_so_no_match_agrupado", "conciliacoes", type_="check"
    )
    op.execute(
        "UPDATE conciliacoes "
        "SET status = 'sem_correspondencia', regra_aplicada = NULL, score_confianca = NULL, "
        "grupo_id = NULL "
        "WHERE status = 'match_agrupado'"
    )
    op.drop_constraint("ck_conciliacoes_regra_aplicada_valida", "conciliacoes", type_="check")
    op.drop_constraint("ck_conciliacoes_status_valido", "conciliacoes", type_="check")
    op.create_check_constraint("ck_conciliacoes_status_valido", "conciliacoes", STATUS_ANTIGOS)
    op.create_check_constraint(
        "ck_conciliacoes_regra_aplicada_valida", "conciliacoes", REGRA_ANTIGA
    )
    op.drop_index(op.f("ix_conciliacoes_grupo_id"), table_name="conciliacoes")
    op.drop_column("conciliacoes", "grupo_id")
