import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.plano import PLANOS
from app.models.base import Base, TimestampMixin


class Empresa(Base, TimestampMixin):
    """O tenant do sistema. Toda tabela com dado de negócio se escopa por empresa_id
    (ver ADR-004, seção multi-tenant — issue #5).

    `cnpj_verificado_em` (issue #119) é quando o cadastro confirmou na Receita
    que o CNPJ existe e está ativo (app/services/consulta_cnpj/). Fica nulo quando a
    consulta estava desligada ou indisponível e nas empresas cadastradas antes
    da #119. Nada mais da consulta é guardado: nem a razão social da Receita,
    nem o quadro de sócios.

    Plano e teste (issue #122, regra em app/core/plano.py): `plano` é o
    escolhido pelo administrador (nulo até a primeira escolha, e nas empresas
    anteriores à #122), `teste_termina_em` é a primeira escolha mais 14 dias e
    não muda quando o plano é trocado, e `assinatura_ativa` marca a empresa
    que paga (hoje, pelo script scripts/ativar_empresa.py). O CHECK garante
    que plano e fim do teste vêm juntos. A situação é calculada, nunca
    gravada."""

    __tablename__ = "empresas"
    __table_args__ = (
        CheckConstraint(
            "plano IN (" + ", ".join(f"'{plano}'" for plano in PLANOS) + ")",
            name="ck_empresas_plano_valido",
        ),
        CheckConstraint(
            "(plano IS NULL) = (teste_termina_em IS NULL)",
            name="ck_empresas_plano_e_fim_do_teste_juntos",
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

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="empresa")  # noqa: F821
    configuracao: Mapped["Configuracao"] = relationship(  # noqa: F821
        back_populates="empresa", uselist=False
    )
    extratos: Mapped[list["Extrato"]] = relationship(back_populates="empresa")  # noqa: F821
