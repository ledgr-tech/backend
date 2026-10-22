import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UsoAssistente(Base):
    """Uma pergunta respondida pelo assistente da Central de ajuda (issue #137,
    ADR-020): só a conta e o instante, para os limites diários por conta e
    global. Nenhum texto. As linhas com mais de 90 dias são apagadas a cada
    gravação (app/services/ajuda/uso.py)."""

    __tablename__ = "usos_assistente"
    __table_args__ = (
        Index("ix_usos_assistente_conta_id_criado_em", "conta_id", "criado_em"),
        Index("ix_usos_assistente_criado_em", "criado_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PerguntaSemResposta(Base):
    """Pergunta que a Central de ajuda não cobriu (issue #137, ADR-020), para o
    time escrever a ajuda que falta (scripts/perguntas_sem_resposta.py).

    Só a pergunta já mascarada (CPF, CNPJ, e-mail, telefone e números longos),
    a tela em que a pessoa estava e o instante. Sem usuário, conta nem
    empresa, de propósito. As linhas com mais de 90 dias são apagadas a cada
    gravação. O texto nunca vai para o log."""

    __tablename__ = "perguntas_sem_resposta"
    __table_args__ = (Index("ix_perguntas_sem_resposta_criado_em", "criado_em"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pergunta: Mapped[str] = mapped_column(Text, nullable=False)
    tela: Mapped[str | None] = mapped_column(String(64), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
