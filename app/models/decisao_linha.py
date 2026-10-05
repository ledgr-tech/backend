import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DecisaoLinha(Base):
    """Evento de decisão sobre uma linha de conciliação (issue #83): conferir,
    justificar e desfazer cada um.

    Só de inserção: nunca editada nem apagada. A decisão em vigor de uma linha
    é derivada da sequência de eventos (`app/services/decisoes.py`).

    Não fica em `conciliacoes` porque `conciliar_extratos` apaga e recria as
    linhas do par a cada rodada (ADR-007). A linha é identificada por
    `(empresa_id, extrato_banco_id, chave)`, onde `chave` é estável entre
    rodadas e entre versões do extrato do sistema (regra em
    `app/services/decisoes.py::chave_da_linha`).

    `usuario_id` é quem decidiu; `autor_nome` é a cópia do nome no momento da
    decisão, e é ela que a API devolve (renomear o usuário depois não reescreve
    o histórico). `rodada` e `extrato_sistema_id` registram em que rodada a
    decisão foi tomada, para auditoria.

    Não usa o `TimestampMixin`: `em` é `timestamptz` (o mixin grava
    `DateTime` sem fuso), porque o front lê a hora com fuso.
    """

    __tablename__ = "decisoes_linha"
    __table_args__ = (
        CheckConstraint(
            "tipo IN ('conferida', 'conferencia_desfeita', 'justificada', "
            "'justificativa_desfeita')",
            name="ck_decisoes_linha_tipo_valido",
        ),
        CheckConstraint(
            "(tipo = 'justificada' AND texto IS NOT NULL) "
            "OR (tipo <> 'justificada' AND texto IS NULL)",
            name="ck_decisoes_linha_texto_so_na_justificativa",
        ),
        CheckConstraint("rodada >= 1", name="ck_decisoes_linha_rodada_positiva"),
        Index(
            "ix_decisoes_linha_empresa_id_extrato_banco_id_chave_em",
            "empresa_id",
            "extrato_banco_id",
            "chave",
            "em",
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
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
