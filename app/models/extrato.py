import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
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
    """

    __tablename__ = "extratos"
    __table_args__ = (
        CheckConstraint("formato IN ('ofx', 'csv')", name="ck_extratos_formato_suportado"),
        CheckConstraint(
            "status IN ('pendente', 'processando', 'concluido', 'erro')",
            name="ck_extratos_status_valido",
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
    quantidade_lancamentos: Mapped[int | None] = mapped_column(Integer, nullable=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="extratos")  # noqa: F821
    lancamentos: Mapped[list["Lancamento"]] = relationship(back_populates="extrato")  # noqa: F821
