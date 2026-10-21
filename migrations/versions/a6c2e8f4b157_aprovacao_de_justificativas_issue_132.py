"""aprovação de justificativas (issue #132, ADR-019)

`decisoes_linha` ganha os tipos `justificativa_aprovada` e
`justificativa_rejeitada`. O texto é obrigatório na `justificada`, opcional
na `justificativa_rejeitada` (o motivo) e proibido nos demais; o CHECK de
texto passa a se chamar `ck_decisoes_linha_texto_valido`.

As justificativas em vigor no deploy ficam aprovadas: para cada linha cujo
último evento é uma `justificada`, entra uma `justificativa_aprovada` com o
`usuario_id` de quem justificou, `autor_nome` "Ledgr (atualização de
21/10)" e a rodada e o extrato do sistema copiados da justificativa. Os
resumos congelados dos fechamentos antigos não mudam.

O downgrade apaga essas aprovações da migração e recusa se sobrar qualquer
evento dos tipos novos (aprovação ou rejeição feita por alguém), porque o
esquema anterior não os comporta e apagar perderia histórico.

Revision ID: a6c2e8f4b157
Revises: f4b8d6e1a923
Create Date: 2026-10-21 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6c2e8f4b157"
down_revision: str | None = "f4b8d6e1a923"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTOR_DA_MIGRACAO = "Ledgr (atualização de 21/10)"
TIPOS_ANTIGOS = "'conferida', 'conferencia_desfeita', 'justificada', 'justificativa_desfeita'"
TIPOS_NOVOS = "'justificativa_aprovada', 'justificativa_rejeitada'"
CHECK_TEXTO_ANTIGO = (
    "(tipo = 'justificada' AND texto IS NOT NULL) " "OR (tipo <> 'justificada' AND texto IS NULL)"
)
CHECK_TEXTO_NOVO = (
    "(tipo = 'justificada' AND texto IS NOT NULL) "
    "OR tipo = 'justificativa_rejeitada' "
    "OR (tipo NOT IN ('justificada', 'justificativa_rejeitada') AND texto IS NULL)"
)


def upgrade() -> None:
    op.drop_constraint("ck_decisoes_linha_tipo_valido", "decisoes_linha", type_="check")
    op.create_check_constraint(
        "ck_decisoes_linha_tipo_valido",
        "decisoes_linha",
        f"tipo IN ({TIPOS_ANTIGOS}, {TIPOS_NOVOS})",
    )
    op.drop_constraint(
        "ck_decisoes_linha_texto_so_na_justificativa", "decisoes_linha", type_="check"
    )
    op.create_check_constraint("ck_decisoes_linha_texto_valido", "decisoes_linha", CHECK_TEXTO_NOVO)

    # A decisão em vigor de uma linha é o último evento dela (antes desta
    # migração não há aprovação nem rejeição): se for uma justificada, aprova.
    op.execute(sa.text("""
            INSERT INTO decisoes_linha (
                id, empresa_id, extrato_banco_id, chave, tipo, texto, usuario_id,
                autor_nome, rodada, extrato_sistema_id
            )
            SELECT
                gen_random_uuid(), ultimo.empresa_id, ultimo.extrato_banco_id, ultimo.chave,
                'justificativa_aprovada', NULL, ultimo.usuario_id, :autor, ultimo.rodada,
                ultimo.extrato_sistema_id
            FROM (
                SELECT DISTINCT ON (empresa_id, extrato_banco_id, chave) *
                FROM decisoes_linha
                ORDER BY empresa_id, extrato_banco_id, chave, em DESC, id DESC
            ) AS ultimo
            WHERE ultimo.tipo = 'justificada'
            """).bindparams(autor=AUTOR_DA_MIGRACAO))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM decisoes_linha WHERE tipo = 'justificativa_aprovada' AND autor_nome = :a"
        ),
        {"a": AUTOR_DA_MIGRACAO},
    )
    restantes = bind.execute(
        sa.text(f"SELECT count(*) FROM decisoes_linha WHERE tipo IN ({TIPOS_NOVOS})")
    ).scalar_one()
    if restantes:
        raise RuntimeError(
            f"Downgrade impossível: {restantes} aprovação(ões) ou rejeição(ões) de "
            "justificativa feitas depois da issue #132 não cabem no esquema anterior."
        )
    op.drop_constraint("ck_decisoes_linha_texto_valido", "decisoes_linha", type_="check")
    op.create_check_constraint(
        "ck_decisoes_linha_texto_so_na_justificativa", "decisoes_linha", CHECK_TEXTO_ANTIGO
    )
    op.drop_constraint("ck_decisoes_linha_tipo_valido", "decisoes_linha", type_="check")
    op.create_check_constraint(
        "ck_decisoes_linha_tipo_valido", "decisoes_linha", f"tipo IN ({TIPOS_ANTIGOS})"
    )
