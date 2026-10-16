import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Extrato(Base, TimestampMixin):
    """Metadados de um upload de extrato (issue #7).

    Por decisão da ADR-002, o arquivo bruto NUNCA é persistido — só guardamos
    metadados (nome do arquivo, formato, tamanho, status, quantidade de
    lançamentos depois de processado). O conteúdo é lido em memória pra
    validar tamanho e, desde a issue #12, também passado pra BackgroundTask
    de normalização (app/services/normalizacao.py); é descartado assim que
    essa task termina de processar, nunca gravado em disco nem em coluna.

    `origem` diz de que lado da conciliação o extrato vem: 'banco' (extrato
    bancário) ou 'sistema' (exportação do sistema de gestão). É obrigatória
    no upload e sem default no model nem no banco (issue #17); o motor de
    matching (ADR-006) usa pra saber qual extrato é de qual lado.

    `periodo_inicio`/`periodo_fim` (issue #82) são a menor e a maior data dos
    lançamentos válidos deste extrato, gravadas por
    `app/services/normalizacao.py` na mesma transação do status final.
    Ficam `NULL` enquanto o extrato está "pendente" ou "processando", e
    continuam `NULL` se o processamento não gerou nenhum lançamento válido
    (status "erro"). O CHECK garante que os dois vêm juntos (ou nenhum) e que
    `periodo_inicio` nunca é depois de `periodo_fim`.

    `saldo_final`/`saldo_data` (issue #118) são o saldo que o próprio arquivo
    informa e a data dele: o LEDGERBAL do OFX, ou a coluna opcional `saldo`
    do CSV (regras em app/parsers/). Gravados pela normalização na mesma
    transação do status final, e `NULL` quando o arquivo não traz saldo
    legível, enquanto processa e nos extratos enviados antes da #118 (o
    arquivo bruto não é guardado, ADR-002, então não há de onde tirar). O
    CHECK garante que os dois vêm juntos (ou nenhum). O saldo inicial não é
    gravado: é calculado na leitura (app/services/saldo.py).
    """

    __tablename__ = "extratos"
    __table_args__ = (
        CheckConstraint("formato IN ('ofx', 'csv')", name="ck_extratos_formato_suportado"),
        CheckConstraint(
            "status IN ('pendente', 'processando', 'concluido', 'concluido_com_erros', 'erro')",
            name="ck_extratos_status_valido",
        ),
        CheckConstraint("origem IN ('banco', 'sistema')", name="ck_extratos_origem_valida"),
        CheckConstraint(
            "(periodo_inicio IS NULL AND periodo_fim IS NULL) "
            "OR (periodo_inicio IS NOT NULL AND periodo_fim IS NOT NULL "
            "AND periodo_inicio <= periodo_fim)",
            name="ck_extratos_periodo_consistente",
        ),
        CheckConstraint(
            "(saldo_final IS NULL AND saldo_data IS NULL) "
            "OR (saldo_final IS NOT NULL AND saldo_data IS NOT NULL)",
            name="ck_extratos_saldo_consistente",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    nome_arquivo: Mapped[str] = mapped_column(String, nullable=False)
    formato: Mapped[str] = mapped_column(String(3), nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pendente")
    origem: Mapped[str] = mapped_column(String, nullable=False)
    quantidade_lancamentos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    periodo_inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    periodo_fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    saldo_final: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    saldo_data: Mapped[date | None] = mapped_column(Date, nullable=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="extratos")  # noqa: F821
    lancamentos: Mapped[list["Lancamento"]] = relationship(back_populates="extrato")  # noqa: F821
    linhas_invalidas: Mapped[list["LinhaInvalida"]] = relationship(  # noqa: F821
        back_populates="extrato"
    )
