"""Servidor de autorização OAuth da conexão MCP (issue #136, ADR-022).

O ChatGPT e o Claude do cliente se registram (`ClienteOAuth`), mandam a
pessoa ao `/oauth/authorize` (`PedidoAutorizacaoOAuth`), que vai à tela de
consentimento do front. Quando um admin ou gestor aprova, nasce a
`ConexaoMcp` (uma empresa só, só leitura) e um `CodigoOAuth`, trocado no
`/oauth/token` por um `TokenOAuth` (acesso e renovação).

Nenhum segredo é gravado: pedido, código e tokens guardam só o sha256 hex do
valor aleatório. Regras em app/services/oauth/ e app/api/oauth.py.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _agora_do_banco() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ClienteOAuth(Base):
    """Cliente público registrado pelo RFC 7591 (`POST /oauth/register`).
    Não é de nenhuma conta: o Claude registra um a cada conexão nova. Os que
    nunca geraram conexão são apagados depois de 7 dias."""

    __tablename__ = "clientes_oauth"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    criado_em: Mapped[datetime] = _agora_do_banco()


class PedidoAutorizacaoOAuth(Base):
    """Um `/oauth/authorize` válido, esperando a decisão na tela do front.
    Expira em 10 minutos e vale uma vez. `conta_id` é preenchido quando
    alguém da conta aprova ou recusa."""

    __tablename__ = "pedidos_autorizacao_oauth"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clientes_oauth.client_id", ondelete="CASCADE"), nullable=False
    )
    redirect_uri: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conta_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=True
    )
    criado_em: Mapped[datetime] = _agora_do_banco()


class ConexaoMcp(Base):
    """Uma conexão aprovada: um cliente OAuth lendo uma empresa da conta, só
    leitura. Revogar (`revogada_em`) corta todos os tokens dela na hora."""

    __tablename__ = "conexoes_mcp"
    __table_args__ = (Index("ix_conexoes_mcp_conta_id_criado_em", "conta_id", "criado_em"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clientes_oauth.client_id"), nullable=False
    )
    # Cópia: o nome continua na lista mesmo que o cliente mude.
    nome_cliente: Mapped[str] = mapped_column(String(100), nullable=False)
    escopo: Mapped[str] = mapped_column(String, nullable=False)
    criada_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    criado_em: Mapped[datetime] = _agora_do_banco()
    ultimo_uso_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revogada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revogada_por_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )


class CodigoOAuth(Base):
    """Código de autorização: 60 segundos, uso único, ligado ao cliente, ao
    redirect_uri, ao `code_challenge` (S256) e ao `resource`."""

    __tablename__ = "codigos_oauth"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    conexao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conexoes_mcp.id"), nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    criado_em: Mapped[datetime] = _agora_do_banco()


class TokenOAuth(Base):
    """Um par emitido pelo `/oauth/token`: o token de acesso (1 hora) e o de
    renovação (30 dias). `codigo_id` é o código de origem, mantido na
    rotação, para o reuso do código revogar tudo o que veio dele.
    `renovado_em` marca o token de renovação já trocado: usá-lo de novo
    revoga a conexão inteira."""

    __tablename__ = "tokens_oauth"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conexao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conexoes_mcp.id"), nullable=False, index=True
    )
    codigo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codigos_oauth.id"), nullable=True, index=True
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_acesso: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    hash_renovacao: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    acesso_expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renovacao_expira_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    renovado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revogado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    criado_em: Mapped[datetime] = _agora_do_banco()
