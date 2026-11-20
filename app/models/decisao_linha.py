import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Todos os tipos de evento (issues #83, #132 e #164). A regra de cada um mora em
# app/services/decisoes.py.
TIPOS_DE_EVENTO = (
    "conferida",
    "conferencia_desfeita",
    "justificada",
    "justificativa_desfeita",
    "justificativa_aprovada",
    "justificativa_rejeitada",
    "aprovacao_desfeita",
)


class DecisaoLinha(Base):
    """Evento de decisão sobre uma linha de conciliação (issue #83): conferir,
    justificar e desfazer cada um. Desde a issue #132, também aprovar e
    rejeitar a justificativa (`justificativa_aprovada` e
    `justificativa_rejeitada`), só por admin ou gestor. Toda justificativa
    nasce aguardando aprovação. Desde a issue #164, `aprovacao_desfeita` volta
    a justificativa aprovada para aguardando aprovação.

    Só de inserção: nunca editada nem apagada. A decisão em vigor de uma linha
    é derivada da sequência de eventos (`app/services/decisoes.py`).

    Não fica em `conciliacoes` porque `conciliar_extratos` apaga e recria as
    linhas do par a cada rodada (ADR-007). A linha é identificada por
    `(empresa_id, extrato_banco_id, chave)`, onde `chave` é estável entre
    rodadas e entre versões do extrato do sistema (regra em
    `app/services/decisoes.py::chave_da_linha`).

    `texto` é obrigatório na `justificada`, opcional na
    `justificativa_rejeitada` (o motivo da rejeição) e proibido nos demais.

    `usuario_id` é quem decidiu; `autor_nome` é a cópia do nome no momento da
    decisão, e é ela que a API devolve (renomear o usuário depois não reescreve
    o histórico). `rodada` e `extrato_sistema_id` registram em que rodada a
    decisão foi tomada, para auditoria.

    `lote_id` (issue #237) liga as justificativas feitas juntas pelo
    `POST /conciliacoes/{extrato_id}/decisoes/lote`: o mesmo uuid em todos os
    eventos `justificada` daquele pedido, e nulo em todo o resto, inclusive na
    `justificativa_aprovada` gravada junto com a flag de aprovação desligada.
    O CHECK só aceita `lote_id` na `justificada`; o índice parcial acha as
    linhas de um lote (o `lote_tamanho` da listagem e a aprovação por
    `lote_id` em `POST /justificativas/lote`).

    Não usa o `TimestampMixin`: `em` é `timestamptz` (o mixin grava
    `DateTime` sem fuso), porque o front lê a hora com fuso. O default é
    `clock_timestamp()`, a hora real do INSERT, porque a decisão em vigor
    depende da ordem por `em`.
    """

    __tablename__ = "decisoes_linha"
    __table_args__ = (
        CheckConstraint(
            "tipo IN (" + ", ".join(f"'{tipo}'" for tipo in TIPOS_DE_EVENTO) + ")",
            name="ck_decisoes_linha_tipo_valido",
        ),
        CheckConstraint(
            "(tipo = 'justificada' AND texto IS NOT NULL) "
            "OR tipo = 'justificativa_rejeitada' "
            "OR (tipo NOT IN ('justificada', 'justificativa_rejeitada') AND texto IS NULL)",
            name="ck_decisoes_linha_texto_valido",
        ),
        CheckConstraint("rodada >= 1", name="ck_decisoes_linha_rodada_positiva"),
        CheckConstraint(
            "lote_id IS NULL OR tipo = 'justificada'",
            name="ck_decisoes_linha_lote_so_na_justificada",
        ),
        Index(
            "ix_decisoes_linha_empresa_id_extrato_banco_id_chave_em",
            "empresa_id",
            "extrato_banco_id",
            "chave",
            "em",
        ),
        Index(
            "ix_decisoes_linha_empresa_id_extrato_banco_id_lote_id",
            "empresa_id",
            "extrato_banco_id",
            "lote_id",
            postgresql_where=text("lote_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False
    )
    extrato_banco_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    chave: Mapped[str] = mapped_column(String(64), nullable=False)
    tipo: Mapped[str] = mapped_column(String, nullable=False)
    texto: Mapped[str | None] = mapped_column(String, nullable=True)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    autor_nome: Mapped[str] = mapped_column(String, nullable=False)
    rodada: Mapped[int] = mapped_column(Integer, nullable=False)
    extrato_sistema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    # clock_timestamp(), não now(): now() é a hora de início da transação, e o
    # POST só pega as travas (do mês e do extrato do banco) depois de outras
    # consultas. A ordem de "em" precisa ser a do INSERT, feito depois delas.
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    # Issue #237: o lote da justificativa feita em lote; nulo fora dele.
    lote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
