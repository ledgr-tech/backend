import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    Mapped,
    ORMExecuteState,
    Session,
    mapped_column,
    relationship,
    with_loader_criteria,
)

from app.models.base import Base, TimestampMixin

# Quanto a empresa removida espera, para ser restaurada, antes de ser apagada.
PRAZO_PARA_APAGAR = timedelta(days=30)


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
    nem o quadro de sócios.

    Remoção (issue #215, ADR-019): `DELETE /empresas/{id}` não apaga a linha, só
    desativa. `removida_em` é o momento, `removida_por_id` e `removida_por_nome`
    dizem quem (o nome é a cópia do momento, como em `fechamentos`) e
    `apagar_em` é `removida_em` + `PRAZO_PARA_APAGAR`: passado esse dia, a
    limpeza (scripts/apagar_empresas_removidas.py) apaga os dados de vez. Tudo
    nulo é empresa ativa; o CHECK obriga os campos a virem juntos. A empresa
    removida some de toda consulta ORM por uma regra só, em
    app/core/empresas_removidas.py; quem precisa enxergá-la (restaurar, listar
    as removidas, o cadastro do mesmo CNPJ, a limpeza) pede com
    `execution_options(incluir_removidas=True)`."""

    __tablename__ = "empresas"
    __table_args__ = (
        UniqueConstraint("conta_id", "cnpj", name="uq_empresas_conta_id_cnpj"),
        # Alvo da FK composta de `empresas_favoritas` (issue #157), que prende o
        # favorito à mesma conta da empresa.
        UniqueConstraint("id", "conta_id", name="uq_empresas_id_conta_id"),
        CheckConstraint(
            "(removida_em IS NULL AND apagar_em IS NULL "
            "AND removida_por_id IS NULL AND removida_por_nome IS NULL) "
            "OR (removida_em IS NOT NULL AND apagar_em IS NOT NULL "
            "AND removida_por_nome IS NOT NULL AND apagar_em > removida_em)",
            name="ck_empresas_remocao_completa",
        ),
        # Só as removidas entram: a limpeza diária procura as vencidas, e a
        # lista de removidas da conta parte do índice de `conta_id`.
        Index(
            "ix_empresas_apagar_em",
            "apagar_em",
            postgresql_where=text("apagar_em IS NOT NULL"),
        ),
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
    removida_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removida_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usuarios.id", name="fk_empresas_removida_por_id_usuarios", ondelete="SET NULL"),
        nullable=True,
    )
    removida_por_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    apagar_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conta: Mapped["Conta"] = relationship(back_populates="empresas")  # noqa: F821
    configuracao: Mapped["Configuracao"] = relationship(  # noqa: F821
        back_populates="empresa", uselist=False
    )
    extratos: Mapped[list["Extrato"]] = relationship(back_populates="empresa")  # noqa: F821


# Opção de execução de quem precisa enxergar as empresas removidas.
INCLUIR_REMOVIDAS = "incluir_removidas"


@event.listens_for(Session, "do_orm_execute")
def esconder_empresas_removidas(estado: ORMExecuteState) -> None:
    """A regra única da issue #215: nenhuma consulta ORM enxerga empresa
    removida, seja ela `select(Empresa)`, `db.get`, um join, uma subconsulta ou
    uma contagem. Uma rota nova, ou um serviço novo, herda a regra sem repeti-la.

    Vale para todo `SELECT` da sessão, inclusive dos scripts, que chamam o
    serviço de exclusão pedindo `incluir_removidas`. Não vale para o recarregamento
    de colunas de um objeto já carregado (`refresh` ou expirado por um commit):
    quem tem a empresa na mão continua tendo, e a remoção não quebra a rota que
    acabou de marcá-la. `UPDATE` e `DELETE` não são filtrados."""
    if (
        estado.is_select
        and not estado.is_column_load
        and not estado.execution_options.get(INCLUIR_REMOVIDAS)
    ):
        estado.statement = estado.statement.options(
            with_loader_criteria(Empresa, Empresa.removida_em.is_(None), include_aliases=True)
        )
