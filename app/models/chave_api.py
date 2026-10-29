import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChaveApi(Base):
    """Chave da API pública (issue #135, ADR-021), criada e revogada por admin
    ou gestor da conta.

    O token é "ledgr_" seguido de `secrets.token_urlsafe(32)`, mostrado uma vez
    só, na criação. Só `hash` (sha256 hex do token inteiro, único) é gravado;
    `prefixo` são os primeiros 8 caracteres aleatórios (depois de "ledgr_"),
    para a pessoa reconhecer a chave na lista. `empresa_id` nulo dá acesso a
    todas as empresas da conta; preenchido, só àquela, que é da conta.

    Não expira. `revogada_em` vale na hora: a chave revogada recebe 401 na
    próxima requisição. `ultimo_uso_em` é atualizado no máximo uma vez por
    minuto (app/core/auth.py). No máximo `MAXIMO_CHAVES_ATIVAS` ativas por
    conta (app/api/chaves_api.py).
    """

    __tablename__ = "chaves_api"
    __table_args__ = (Index("ix_chaves_api_conta_id_criado_em", "conta_id", "criado_em"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    empresa_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=True
    )
    nome: Mapped[str] = mapped_column(String(60), nullable=False)
    prefixo: Mapped[str] = mapped_column(String(8), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    criada_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ultimo_uso_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revogada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revogada_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
