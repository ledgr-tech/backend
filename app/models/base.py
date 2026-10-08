"""Base declarativa e mixin de timestamp compartilhados por todos os models.

Ver ADR-004 (02-decisoes/04-schema-usuarios-empresas-configuracoes.md) no
vault de documentação pro contexto completo dessas decisões.
"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """`criado_em`/`atualizado_em` automáticos, calculados pelo Postgres (não pela aplicação)."""

    criado_em: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    atualizado_em: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
