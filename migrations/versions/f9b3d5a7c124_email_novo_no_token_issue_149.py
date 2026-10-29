"""e-mail novo no token da troca de e-mail (issue #149)

`tokens_email.email_novo`, obrigatório quando `finalidade = 'troca_email'` e
nulo nas outras (`ck_tokens_email_email_novo`). A troca de e-mail nunca teve
rota, então não deveria haver token dessa finalidade; se houver algum, ele
não tem o e-mail novo, não serve para nada e é apagado antes do CHECK.

Revision ID: f9b3d5a7c124
Revises: e8a2c4f6b913
Create Date: 2026-10-22 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9b3d5a7c124"
down_revision: str | None = "e8a2c4f6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tokens_email", sa.Column("email_novo", sa.String(), nullable=True))
    op.execute("DELETE FROM tokens_email WHERE finalidade = 'troca_email'")
    op.create_check_constraint(
        "ck_tokens_email_email_novo",
        "tokens_email",
        "(finalidade = 'troca_email') = (email_novo IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tokens_email_email_novo", "tokens_email", type_="check")
    op.drop_column("tokens_email", "email_novo")
