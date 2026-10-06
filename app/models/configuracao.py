import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Configuracao(Base, TimestampMixin):
    """1:1 com empresa (decisão da issue #4). Guarda os thresholds default do
    motor de matching por empresa.

    Desde a issue #85, `tolerancia_dias_default` é editável pelo usuário em
    `PUT /empresa/configuracoes` (app/api/empresa.py), de 0 a 5 dias. O banco
    só garante o mínimo (CHECK `>= 0`); o máximo é regra de produto da API,
    para mudar sem migração. `atualizado_por_id` e `atualizado_por_nome` (cópia
    do nome no momento da alteração, como `autor_nome` em `decisoes_linha`)
    só são preenchidos quando alguém altera pela rota; `atualizado_por_id`
    vira NULL se o usuário for apagado, e o nome fica.

    Só a tolerância em dias é exposta. `similaridade_minima_default` continua
    só desempatando pares (ADR-006 e ADR-008) e não aparece na API.
    """

    __tablename__ = "configuracoes"
    __table_args__ = (
        CheckConstraint(
            "tolerancia_dias_default >= 0", name="ck_configuracoes_tolerancia_dias_nao_negativa"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, unique=True
    )
    tolerancia_dias_default: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    similaridade_minima_default: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="85.00"
    )
    atualizado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id",
            name="fk_configuracoes_atualizado_por_id_usuarios",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    atualizado_por_nome: Mapped[str | None] = mapped_column(String, nullable=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="configuracao")  # noqa: F821
