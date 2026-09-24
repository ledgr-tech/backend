import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ExplicacaoDivergencia(Base, TimestampMixin):
    """Cache de explicações de divergência geradas por IA (issue #28,
    ADR-011).

    Uma linha por explicação gerada com sucesso — um cache hit (mesma
    `chave`, ver app/services/ia/cache.py) nunca gasta nem grava de novo.
    `chave` é o sha256 hex do contexto canônico já mascarado
    (app/services/ia/prompt.py:serializar_contexto), combinado com
    `empresa_id`, provedor, modelo e versão do prompt: trocar de provedor
    ou modelo, ou mudar o texto do prompt, invalida o cache sozinho (entra
    num hash novo, nunca precisa de migração de dado). UNIQUE
    (`empresa_id`, `chave`): nenhuma empresa reaproveita explicação de
    outra (isolamento por tenant, ADR-004), e um INSERT concorrente da
    mesma chave é resolvido por `ON CONFLICT DO NOTHING` no código (issue
    #28), não por checagem prévia na aplicação.

    O contexto enviado à IA NUNCA é gravado aqui — só o texto da explicação
    e a contagem de tokens. Não existe coluna de descrição, valor ou data
    de lançamento nesta tabela.

    `criado_em` (`TimestampMixin`) é a base da contagem diária de uso: por
    empresa (índice composto com `empresa_id`, `ix_explicacoes_divergencia_
    empresa_id_criado_em`) e global (índice próprio em `criado_em`),
    contadas por dia UTC — só geração nova conta pro limite, cache hit não
    conta (issue #28).
    """

    __tablename__ = "explicacoes_divergencia"
    __table_args__ = (
        UniqueConstraint("empresa_id", "chave", name="uq_explicacoes_divergencia_empresa_id_chave"),
        Index("ix_explicacoes_divergencia_empresa_id_criado_em", "empresa_id", "criado_em"),
        Index("ix_explicacoes_divergencia_criado_em", "criado_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    chave: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    provedor: Mapped[str] = mapped_column(String, nullable=False)
    modelo: Mapped[str] = mapped_column(String, nullable=False)
    versao_prompt: Mapped[str] = mapped_column(String, nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_entrada: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_saida: Mapped[int] = mapped_column(Integer, nullable=False)
