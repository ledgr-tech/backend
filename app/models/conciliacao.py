import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Conciliacao(Base, TimestampMixin):
    """Resultado da conciliação de um lançamento do banco com um do sistema
    (issue #17). Cada linha é um par (`match_exato`, `match_tolerancia`,
    divergências) ou um lançamento sozinho (`sem_correspondencia`,
    `duplicado`), por isso as duas pontas de lançamento são nullable, com o
    CHECK garantindo que pelo menos uma exista.

    `empresa_id` é coluna própria e indexada, mesmo alcançável via
    `extrato_banco_id`, porque toda query real filtra por tenant (ADR-004,
    seção multi-tenant). `extrato_sistema_id` não tem índice próprio nesta
    issue; as consultas partem do extrato do banco.

    `regra_aplicada` e `score_confianca` são colunas de auditoria: dizem qual
    regra do motor gerou a linha e com que confiança. Ficam nulas quando não
    se aplicam (ex: `sem_correspondencia`). O preenchimento pelo motor exato
    vem na issue #16.

    `status` cobre as 5 categorias de divergência da issue #24, além dos dois
    status de match: `match_exato`/`match_tolerancia` (par formado, ADR-006/
    ADR-008), `duplicado` (excedente de um grupo do motor exato que já
    formou par, decidido só na ADR-006), e a sub-classificação do que sobra
    sem par depois de todas as passadas — `tarifa_bancaria` (descrição bate
    termo conhecido, só lado banco), `divergente_valor` (achou algo na mesma
    data do outro lado, valor não bate), `divergente_data` (achou o mesmo
    valor do outro lado, data não bate) e `sem_correspondencia` (nenhum dos
    anteriores). Ver docstring de app/services/matching.py.

    Sem UniqueConstraint de propósito: a idempotência de reexecutar a
    conciliação do mesmo par de extratos é responsabilidade do motor (#16).
    """

    __tablename__ = "conciliacoes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('match_exato', 'match_tolerancia', 'divergente_valor', "
            "'divergente_data', 'sem_correspondencia', 'duplicado', 'tarifa_bancaria')",
            name="ck_conciliacoes_status_valido",
        ),
        CheckConstraint(
            "regra_aplicada IS NULL OR regra_aplicada IN ('exato', 'tolerancia', 'fuzzy_descricao')",
            name="ck_conciliacoes_regra_aplicada_valida",
        ),
        CheckConstraint(
            "score_confianca IS NULL OR score_confianca BETWEEN 0 AND 1",
            name="ck_conciliacoes_score_confianca_entre_0_e_1",
        ),
        CheckConstraint(
            "lancamento_banco_id IS NOT NULL OR lancamento_sistema_id IS NOT NULL",
            name="ck_conciliacoes_ao_menos_um_lancamento",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    extrato_banco_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False, index=True
    )
    extrato_sistema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    lancamento_banco_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lancamentos.id"), nullable=True, index=True
    )
    lancamento_sistema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lancamentos.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    regra_aplicada: Mapped[str | None] = mapped_column(String, nullable=True)
    score_confianca: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
