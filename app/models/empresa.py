import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Empresa(Base, TimestampMixin):
    """Empresa cliente de uma conta (issue #133, ADR-019). Toda tabela com dado
    de negócio se escopa por empresa_id (ver ADR-004, seção multi-tenant —
    issue #5): extratos, conciliações, justificativas, fechamentos e a
    configuração ficam separados por empresa. Usuários, convites e plano são
    da conta (app/models/conta.py).

    O CNPJ é único dentro da conta (`uq_empresas_conta_id_cnpj`), não no
    sistema: a mesma empresa pode ser cliente de dois escritórios e ainda ter
    conta própria. A empresa ativa de cada requisição chega no cabeçalho
    `X-Ledgr-Empresa` (app/core/auth.py).

    `cnpj_verificado_em` (issue #119) é quando o cadastro confirmou na Receita
    que o CNPJ existe e está ativo (app/services/consulta_cnpj/). Fica nulo quando a
    consulta estava desligada ou indisponível e nas empresas cadastradas antes
    da #119. Nada mais da consulta é guardado: nem a razão social da Receita,
    nem o quadro de sócios."""

    __tablename__ = "empresas"
    __table_args__ = (
        UniqueConstraint("conta_id", "cnpj", name="uq_empresas_conta_id_cnpj"),
        # Alvo da FK composta de `empresas_favoritas` (issue #157), que prende o
        # favorito à mesma conta da empresa.
        UniqueConstraint("id", "conta_id", name="uq_empresas_id_conta_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False, index=True
    )
    razao_social: Mapped[str] = mapped_column(String, nullable=False)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False)
    cnpj_verificado_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conta: Mapped["Conta"] = relationship(back_populates="empresas")  # noqa: F821
    configuracao: Mapped["Configuracao"] = relationship(  # noqa: F821
        back_populates="empresa", uselist=False
    )
    extratos: Mapped[list["Extrato"]] = relationship(back_populates="empresa")  # noqa: F821
