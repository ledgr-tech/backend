"""Painel administrativo interno (issue #156, ADR-025): operadores do Ledgr,
registro de acesso ao `/admin` e eventos das contas."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

RESULTADOS_DE_ACESSO = ("permitido", "nao_operador", "login_antigo")
TIPOS_DE_EVENTO_DA_CONTA = ("inativada", "reativada", "piloto_ligado", "piloto_desligado")
ORIGENS_DE_EVENTO = ("painel", "script")


def _lista(valores: tuple[str, ...]) -> str:
    return ", ".join(f"'{valor}'" for valor in valores)


class OperadorLedgr(Base):
    """Usuário da equipe do Ledgr que acessa o `/admin` (issue #156, ADR-025).

    Fora dos papéis da conta (`usuarios.papel`) e fora do JWT: o backend
    confere esta tabela a cada requisição do `/admin`, e remover vale na
    próxima. Incluído e removido só por scripts/operadores.py, nunca por API.
    Remover não apaga: preenche `removido_em`, e incluir de novo cria outra
    linha (o índice único parcial deixa só um ativo por usuário).
    `incluido_por` e `removido_por` são o nome de quem rodou o script.
    """

    __tablename__ = "operadores_ledgr"
    __table_args__ = (
        Index(
            "uq_operadores_ledgr_usuario_id_ativo",
            "usuario_id",
            unique=True,
            postgresql_where=text("removido_em IS NULL"),
        ),
        CheckConstraint(
            "(removido_em IS NULL AND removido_por IS NULL) "
            "OR (removido_em IS NOT NULL AND removido_por IS NOT NULL)",
            name="ck_operadores_ledgr_remocao_completa",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    incluido_por: Mapped[str] = mapped_column(String(100), nullable=False)
    incluido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    removido_por: Mapped[str | None] = mapped_column(String(100), nullable=True)
    removido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AcessoAdmin(Base):
    """Uma requisição ao `/admin` com sessão válida (issue #156, ADR-025), só
    de inserção. Inclui as negadas (`nao_operador`, `login_antigo`), para uma
    tentativa de escalada aparecer.

    `rota` é o modelo do caminho (`/admin/contas/{conta_id}`), nunca a URL com
    a busca, que pode trazer um e-mail. Nada do corpo nem da resposta. Sem FK:
    o registro sobrevive à exclusão da conta consultada e à do usuário, como
    `registros_exclusao`; por isso a coluna é `conta_consultada_id`, e não
    `conta_id`. Guardado por `RETENCAO_ACESSOS` (app/api/admin.py).
    """

    __tablename__ = "acessos_admin"
    __table_args__ = (
        CheckConstraint(
            f"resultado IN ({_lista(RESULTADOS_DE_ACESSO)})", name="ck_acessos_admin_resultado"
        ),
        Index("ix_acessos_admin_em", "em"),
        Index("ix_acessos_admin_conta_consultada_id_em", "conta_consultada_id", "em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operador_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metodo: Mapped[str] = mapped_column(String(10), nullable=False)
    rota: Mapped[str] = mapped_column(String(200), nullable=False)
    conta_consultada_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resultado: Mapped[str] = mapped_column(String(20), nullable=False)
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


class EventoConta(Base):
    """Inativação, reativação e piloto ativo de uma conta (issue #156,
    ADR-025), só de inserção. O estado atual fica em `contas`
    (`inativada_em`, `assinatura_ativa`); aqui, quem fez, quando e por quê.

    `operador_id` é nulo quando veio do script (`origem` "script"). O nome do
    operador é copiado, como nos fechamentos: o id vira `NULL` se o operador
    for apagado com a conta dele.
    """

    __tablename__ = "eventos_conta"
    __table_args__ = (
        CheckConstraint(
            f"tipo IN ({_lista(TIPOS_DE_EVENTO_DA_CONTA)})", name="ck_eventos_conta_tipo"
        ),
        CheckConstraint(f"origem IN ({_lista(ORIGENS_DE_EVENTO)})", name="ck_eventos_conta_origem"),
        Index("ix_eventos_conta_conta_id_em", "conta_id", "em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False
    )
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    motivo: Mapped[str] = mapped_column(String(500), nullable=False)
    operador_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "operadores_ledgr.id",
            name="fk_eventos_conta_operador_id_operadores_ledgr",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    operador_nome: Mapped[str | None] = mapped_column(String, nullable=True)
    origem: Mapped[str] = mapped_column(String(10), nullable=False)
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
