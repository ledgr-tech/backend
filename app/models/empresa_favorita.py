import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, PrimaryKeyConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EmpresaFavorita(Base):
    """Empresa que um usuário marcou como favorita no seletor (issue #157).

    É do usuário, não da conta: cada pessoa do escritório favorita as empresas
    que acompanha, e os favoritos de uma não aparecem para outra. A linha
    existe ou não; desfavoritar apaga.

    `conta_id` está aqui só para a constraint: as duas FKs compostas,
    `(usuario_id, conta_id)` para `usuarios` e `(empresa_id, conta_id)` para
    `empresas`, obrigam o usuário e a empresa a serem da mesma conta. Um
    favorito de empresa de outra conta não chega a existir, nem por um bug no
    serviço. As duas são `ON DELETE CASCADE`: apagar a empresa ou a linha do
    usuário apaga os favoritos. A remoção de usuário não apaga a linha dele
    (app/services/usuarios.py), então apaga os favoritos por conta própria.

    Não usa o `TimestampMixin`: só há `criado_em`, e em `timestamptz`.
    """

    __tablename__ = "empresas_favoritas"
    __table_args__ = (
        PrimaryKeyConstraint("usuario_id", "empresa_id", name="pk_empresas_favoritas"),
        ForeignKeyConstraint(
            ["usuario_id", "conta_id"],
            ["usuarios.id", "usuarios.conta_id"],
            name="fk_empresas_favoritas_usuario_da_conta",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["empresa_id", "conta_id"],
            ["empresas.id", "empresas.conta_id"],
            name="fk_empresas_favoritas_empresa_da_conta",
            ondelete="CASCADE",
        ),
    )

    usuario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    empresa_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    conta_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
