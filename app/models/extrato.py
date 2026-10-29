import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Importação via IA ainda não confirmada (issue #158, ADR-013): fora de toda
# conciliação, listagem e contagem, como se não existisse.
STATUS_RASCUNHO = "rascunho"


class Extrato(Base, TimestampMixin):
    """Metadados de um upload de extrato (issue #7).

    Por decisão da ADR-002, o arquivo bruto NUNCA é persistido no banco — só
    guardamos metadados (nome do arquivo, formato, tamanho, status, quantidade
    de lançamentos depois de processado). O conteúdo é lido em memória pra
    validar tamanho e, desde a issue #12, também passado pra BackgroundTask
    de normalização (app/services/normalizacao.py); é descartado assim que
    essa task termina de processar, nunca gravado em disco nem em coluna.

    `arquivo_chave` (issue #139, ADR-023) é a chave do objeto no bucket do R2
    quando o arquivo original foi guardado: só no plano avançado, com
    `ARQUIVO_ORIGINAL_HABILITADO` ligada (app/services/arquivo_original.py).
    O arquivo fica cifrado no R2, nunca no banco. `NULL` quando não há
    original: outros planos, falha no envio e todos os extratos de antes.

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

    Importação via IA (issue #158, ADR-013): o relatório do sistema de gestão
    convertido pela IA do próprio usuário entra com `formato` "json" e
    `status` "rascunho" (`STATUS_RASCUNHO`), gravado na hora pela rota, sem
    BackgroundTask (app/services/importacao_ia). O rascunho fica fora de
    toda conciliação, listagem e contagem, e as rotas de extrato e de
    conciliação respondem 404 para ele. Expira em 7 dias e some ao ser
    descartado. A confirmação (#159) o torna um extrato do sistema como os
    outros. O índice parcial `ix_extratos_rascunho_criado_em` serve à limpeza
    dos vencidos.

    `confirmado_por_id`, `confirmado_por_nome` e `confirmado_em` (issue #159)
    registram quem confirmou a importação via IA e quando: a conferência é de
    quem confirma (ADR-013). O nome é a cópia do momento, como em
    `fechamentos`, e o id vira `NULL` se o usuário for apagado. Nulos nos
    outros extratos e no rascunho.
    """

    __tablename__ = "extratos"
    __table_args__ = (
        CheckConstraint("formato IN ('ofx', 'csv', 'json')", name="ck_extratos_formato_suportado"),
        CheckConstraint(
            "status IN ('pendente', 'processando', 'concluido', 'concluido_com_erros', 'erro', "
            "'rascunho')",
            name="ck_extratos_status_valido",
        ),
        CheckConstraint(
            "(confirmado_em IS NULL AND confirmado_por_nome IS NULL AND confirmado_por_id IS NULL) "
            "OR (confirmado_em IS NOT NULL AND confirmado_por_nome IS NOT NULL)",
            name="ck_extratos_confirmacao_consistente",
        ),
        Index(
            "ix_extratos_rascunho_criado_em",
            "criado_em",
            postgresql_where=text("status = 'rascunho'"),
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
    formato: Mapped[str] = mapped_column(String(4), nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pendente")
    origem: Mapped[str] = mapped_column(String, nullable=False)
    quantidade_lancamentos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    periodo_inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    periodo_fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    saldo_final: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    saldo_data: Mapped[date | None] = mapped_column(Date, nullable=True)
    arquivo_chave: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmado_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "usuarios.id", name="fk_extratos_confirmado_por_id_usuarios", ondelete="SET NULL"
        ),
        nullable=True,
    )
    confirmado_por_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="extratos")  # noqa: F821
    lancamentos: Mapped[list["Lancamento"]] = relationship(back_populates="extrato")  # noqa: F821
    linhas_invalidas: Mapped[list["LinhaInvalida"]] = relationship(  # noqa: F821
        back_populates="extrato"
    )
