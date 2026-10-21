import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Papéis na conta (issue #133, ADR-019). A lista única: o CHECK de
# `usuarios.papel`, o `Papel` e a dependency `exigir_papel` saem dela.
PAPEIS = ("admin", "gestor", "analista")
Papel = Literal["admin", "gestor", "analista"]

# Domínio do e-mail que o usuário removido recebe no lugar do dele (issue #69).
# `.invalid` é reservado (RFC 2606): nunca vira endereço de entrega.
DOMINIO_EMAIL_REMOVIDO = "ledgr.invalid"


class Usuario(Base, TimestampMixin):
    """Usuário pertence a uma única conta (ver ADR-003; desde a issue #133, a
    conta, e não mais a empresa, ADR-019).

    Suporta os dois métodos de login decididos na issue #4: credenciais
    (email + senha_hash) e Google OAuth (google_sub). Pelo menos um dos
    dois precisa estar preenchido, a não ser no usuário removido — ver a
    CheckConstraint abaixo.

    `papel` (issue #133, ADR-019) é 'admin', 'gestor' ou 'analista'. O admin
    é quem cadastrou a conta pelo `/register`, e a conta tem exatamente um: o
    índice único parcial garante no máximo um, e as regras de remoção e
    desativação (e, na parte 2 da #133, a troca de papel e a transferência)
    garantem que ele nunca falta. Antes da #133 era o booleano
    `administrador` (issue #69, ADR-015): a migração fez o administrador
    virar 'admin' e os convidados, 'gestor'.

    Remover um usuário não apaga a linha (`decisoes_linha.usuario_id` aponta
    para ela, e o histórico precisa de quem fez o quê): preenche `removido_em`,
    mantém o nome, apaga senha e `google_sub` e troca o e-mail por
    `removido+<id>@ledgr.invalid`, que libera o endereço para outro cadastro
    ou convite. Regra em app/services/usuarios.py.

    `sessoes_validas_desde` encerra as sessões abertas: a troca de senha, a
    redefinição pelo link e a remoção gravam o instante, e o token com
    login anterior a ele deixa de valer (issue #75).
    """

    __tablename__ = "usuarios"
    __table_args__ = (
        CheckConstraint(
            "(removido_em IS NULL AND (senha_hash IS NOT NULL OR google_sub IS NOT NULL)) "
            "OR (removido_em IS NOT NULL AND senha_hash IS NULL AND google_sub IS NULL)",
            name="ck_usuarios_tem_metodo_auth",
        ),
        CheckConstraint(
            "papel IN (" + ", ".join(f"'{papel}'" for papel in PAPEIS) + ")",
            name="ck_usuarios_papel_valido",
        ),
        Index(
            "uq_usuarios_conta_id_admin",
            "conta_id",
            unique=True,
            postgresql_where=text("papel = 'admin'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    senha_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    papel: Mapped[str] = mapped_column(String, nullable=False)
    removido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Token com `auth_time` anterior a isto é recusado (issue #75, ADR-016).
    # Nulo: nenhuma sessão foi encerrada. Gravado no relógio do backend, o
    # mesmo do `auth_time` do login. Ver app/core/auth.py.
    sessoes_validas_desde: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conta: Mapped["Conta"] = relationship(back_populates="usuarios")  # noqa: F821
