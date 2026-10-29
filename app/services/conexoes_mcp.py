"""Revogação das conexões MCP (issue #136, ADR-022).

Revogar a conexão corta na hora todos os tokens dela: a dependency do token
de acesso e a renovação conferem `conexoes_mcp.revogada_em`, e os tokens
ficam marcados também. A conexão morre sozinha quando quem a criou deixa de
poder autorizar: removido, desativou a conta (`remover_usuario`) ou rebaixado
a analista (troca de papel). Nada aqui faz commit: quem chama decide a
transação.
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import ConexaoMcp, TokenOAuth

# Quem pode aprovar uma conexão e mantê-la viva.
PAPEIS_QUE_AUTORIZAM = ("admin", "gestor")
MAXIMO_CONEXOES_ATIVAS = 10


def _revogar(db: Session, condicao, revogada_por_id: uuid.UUID | None) -> None:
    ids = select(ConexaoMcp.id).where(condicao, ConexaoMcp.revogada_em.is_(None))
    db.execute(
        update(TokenOAuth)
        .where(TokenOAuth.conexao_id.in_(ids), TokenOAuth.revogado_em.is_(None))
        .values(revogado_em=func.now())
    )
    db.execute(
        update(ConexaoMcp)
        .where(condicao, ConexaoMcp.revogada_em.is_(None))
        .values(revogada_em=func.now(), revogada_por_id=revogada_por_id)
    )


def revogar_conexao(
    db: Session, conexao_id: uuid.UUID, revogada_por_id: uuid.UUID | None = None
) -> None:
    _revogar(db, ConexaoMcp.id == conexao_id, revogada_por_id)


def revogar_conexoes_do_criador(
    db: Session, usuario_id: uuid.UUID, revogada_por_id: uuid.UUID | None = None
) -> None:
    _revogar(db, ConexaoMcp.criada_por_id == usuario_id, revogada_por_id)


def revogar_se_nao_autoriza_mais(
    db: Session, usuario_id: uuid.UUID, papel: str, revogada_por_id: uuid.UUID | None
) -> None:
    """Depois de uma troca de papel: o rebaixado a analista perde as conexões
    que criou."""
    if papel not in PAPEIS_QUE_AUTORIZAM:
        revogar_conexoes_do_criador(db, usuario_id, revogada_por_id)
