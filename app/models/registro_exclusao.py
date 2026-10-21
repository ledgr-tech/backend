import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RegistroExclusao(Base):
    """Registro de cada exclusão dos dados de uma conta (issue #68, ADR-017),
    gravado por scripts/excluir_empresa.py na mesma transação da exclusão.

    É a prova de que o pedido foi atendido, sem guardar nada do que foi
    apagado: nem razão social, nem nome, nem e-mail. `empresa_id` fica sem FK,
    porque o tenant deixou de existir. Desde a issue #133 (ADR-019), o script
    exclui a conta inteira, e `empresa_id` guarda o id da CONTA (o nome da
    coluna ficou; nas exclusões anteriores, o id da empresa é o mesmo da conta
    que a migração criou dela). `cnpj_sha256` é o sha256 hex do CNPJ da conta,
    para responder depois "a conta deste CNPJ foi excluída?" sem guardar o
    CNPJ. `contagens` é o número de linhas apagadas por tabela, inclusive
    `empresas` e `contas`.
    `executado_por` é o texto digitado por quem rodou o script.

    Sem `TimestampMixin`: a linha nunca é alterada, e `executado_em` é o único
    instante que importa.
    """

    __tablename__ = "registros_exclusao"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cnpj_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contagens: Mapped[dict] = mapped_column(JSONB, nullable=False)
    executado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    executado_por: Mapped[str] = mapped_column(String, nullable=False)
