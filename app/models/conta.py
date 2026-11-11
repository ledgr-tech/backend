import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.localizacao import CIDADE_MAXIMO, CIDADE_MINIMO, UFS
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
    `empresas` na #133: `teste_termina_em` é nulo até o teste começar. Com
    TESTE_NO_CADASTRO_HABILITADO, é o cadastro mais 14 dias (issues #166 e
    #167, sem plano e sem cartão); contas existentes recebem o teste pelo
    script de lançamento da cobrança. O fim não muda
    quando o plano é trocado; `plano` é só a preferência gravada pelo
    `POST /empresa/plano` (obsoleto): o plano que vale é o da assinatura
    quando ela existe (`plano_vigente`); `assinatura_ativa` marca o piloto, que
    não paga (pelo scripts/ativar_empresa.py e pelo `/admin`). A cobrança é a
    `assinatura_stripe`, mantida só pelo webhook, e `stripe_customer_id` é o
    cliente da conta na Stripe, criado no primeiro checkout. A situação é
    calculada, nunca gravada.

    Localização (issue #184): `cidade` e `uf`, nulas, informadas no cadastro ou
    vindas da consulta de CNPJ (regras em app/core/localizacao.py). Dado da
    conta, nunca da empresa cliente; as contas antigas ficam com nulo.

    As contas que existiam antes da #133 têm o mesmo id da empresa de que
    vieram: o JWT já emitido leva esse id no claim `empresa_id`, que passou a
    ser lido como o id da conta (app/core/auth.py).

    Inativação (issue #156, ADR-025): `inativada_em` e `motivo_inativacao`,
    gravados pelo operador do Ledgr no `/admin`. Fica à parte da situação
    comercial, que continua calculada como antes: a conta inativa fica
    somente leitura (app/core/conta_inativa.py), e reativar devolve tudo. Quem
    inativou, reativou ou ligou o piloto fica em `eventos_conta`."""

    __tablename__ = "contas"
    __table_args__ = (
        UniqueConstraint("stripe_customer_id", name="uq_contas_stripe_customer_id"),
        CheckConstraint(
            "plano IN (" + ", ".join(f"'{plano}'" for plano in PLANOS) + ")",
            name="ck_contas_plano_valido",
        ),
        CheckConstraint(
            "(inativada_em IS NULL) = (motivo_inativacao IS NULL)",
            name="ck_contas_inativacao_completa",
        ),
        CheckConstraint(
            f"cidade IS NULL OR char_length(cidade) BETWEEN {CIDADE_MINIMO} AND {CIDADE_MAXIMO}",
            name="ck_contas_cidade_tamanho",
        ),
        CheckConstraint(
            "uf IS NULL OR uf IN (" + ", ".join(f"'{uf}'" for uf in UFS) + ")",
            name="ck_contas_uf_valida",
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
    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    assinatura_ativa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    cidade: Mapped[str | None] = mapped_column(String, nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    inativada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    motivo_inativacao: Mapped[str | None] = mapped_column(String(500), nullable=True)

    empresas: Mapped[list["Empresa"]] = relationship(back_populates="conta")  # noqa: F821
    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="conta")  # noqa: F821
    assinatura_stripe: Mapped["AssinaturaStripe | None"] = relationship(  # noqa: F821
        back_populates="conta", uselist=False
    )
