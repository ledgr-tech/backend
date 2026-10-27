"""desfazer aprovação de justificativa (issue #164)

`decisoes_linha` ganha o tipo `aprovacao_desfeita`, que volta a justificativa
aprovada para aguardando aprovação. Sem texto, como a aprovação: o CHECK de
texto já proíbe texto em todo tipo que não é `justificada` nem
`justificativa_rejeitada`, e não muda.

O downgrade recusa se houver algum `aprovacao_desfeita`, porque o esquema
anterior não o comporta e apagar perderia histórico.

Revision ID: e7a3c5b9d214
Revises: d4b8f1e2a637
Create Date: 2026-10-27 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a3c5b9d214"
down_revision: str | None = "d4b8f1e2a637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIPOS_ANTIGOS = (
    "'conferida', 'conferencia_desfeita', 'justificada', 'justificativa_desfeita', "
    "'justificativa_aprovada', 'justificativa_rejeitada'"
)
TIPO_NOVO = "'aprovacao_desfeita'"


def upgrade() -> None:
    op.drop_constraint("ck_decisoes_linha_tipo_valido", "decisoes_linha", type_="check")
    op.create_check_constraint(
        "ck_decisoes_linha_tipo_valido",
        "decisoes_linha",
        f"tipo IN ({TIPOS_ANTIGOS}, {TIPO_NOVO})",
    )


def downgrade() -> None:
    restantes = (
        op.get_bind()
        .execute(sa.text(f"SELECT count(*) FROM decisoes_linha WHERE tipo = {TIPO_NOVO}"))
        .scalar_one()
    )
    if restantes:
        raise RuntimeError(
            f"Downgrade impossível: {restantes} aprovação(ões) desfeita(s) depois da "
            "issue #164 não cabem no esquema anterior."
        )
    op.drop_constraint("ck_decisoes_linha_tipo_valido", "decisoes_linha", type_="check")
    op.create_check_constraint(
        "ck_decisoes_linha_tipo_valido", "decisoes_linha", f"tipo IN ({TIPOS_ANTIGOS})"
    )
