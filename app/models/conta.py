import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.plano import PLANOS
from app.models.base import Base, TimestampMixin


class Conta(Base, TimestampMixin):
    """O tenant do sistema desde a issue #133 (ADR-019): quem assina o Ledgr.

    Um escritório contábil ou uma empresa que cuida do próprio financeiro. A
    conta tem os usuários (`usuarios.conta_id`), os convites e o plano, e uma
    ou mais empresas clientes (`empresas.conta_id`), que são as donas dos
    dados de negócio. O CNPJ da conta é único no sistema; o da empresa
    cliente, só dentro da conta (a mesma empresa pode estar em várias contas).

    `cnpj_verificado_em` (issue #119) é quando o cadastro confirmou na Receita
    que o CNPJ existe e está ativo; nulo quando a consulta estava desligada ou
    indisponível e nas contas cadastradas antes da #119.

    Plano e teste (issue #122, regra em app/core/plano.py), vindos de
    `empresas` na #133: `plano` é o escolhido pelo admin (nulo até a primeira
    escolha), `teste_termina_em` é a primeira escolha mais 14 dias e não muda
    quando o plano é trocado, e `assinatura_ativa` marca a conta que paga
    (hoje, pelo script scripts/ativar_empresa.py). O CHECK garante que plano e
    fim do teste vêm juntos. A situação é calculada, nunca gravada.

    As contas que existiam antes da #133 têm o mesmo id da empresa de que
    vieram: o JWT já emitido leva esse id no claim `empresa_id`, que passou a
    ser lido como o id da conta (app/core/auth.py)."""

    __tablename__ = "contas"
    __table_args__ = (
        CheckConstraint(
            "plano IN (" + ", ".join(f"'{plano}'" for plano in PLANOS) + ")",
            name="ck_contas_plano_valido",
        ),
        CheckConstraint(
            "(plano IS NULL) = (teste_termina_em IS NULL)",
            name="ck_contas_plano_e_fim_do_teste_juntos",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    razao_social: Mapped[str] = mapped_column(String, nullable=False)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False, unique=True)
    cnpj_verificado_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    plano: Mapped[str | None] = mapped_column(String, nullable=True)
    teste_termina_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assinatura_ativa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    empresas: Mapped[list["Empresa"]] = relationship(back_populates="conta")  # noqa: F821
    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="conta")  # noqa: F821
