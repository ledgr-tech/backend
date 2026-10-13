"""Regras do convite compartilhadas entre as rotas do administrador
(app/api/equipe.py) e as públicas (app/api/convites.py), issue #69, ADR-015.
"""

import hashlib

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models import Convite

VALIDADE_CONVITE_DIAS = 7


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def condicoes_pendente() -> list[ColumnElement[bool]]:
    """Convite que ainda vale: nem usado, nem cancelado, nem expirado."""
    return [
        Convite.usado_em.is_(None),
        Convite.cancelado_em.is_(None),
        Convite.expira_em > func.now(),
    ]


def convite_pendente_pelo_token(db: Session, token: str, *, travar: bool) -> Convite | None:
    """O convite pendente desse token, ou None. Com `travar`, `FOR UPDATE`:
    dois aceites simultâneos do mesmo link não passam os dois. O segundo
    espera, e o Postgres reavalia as condições na linha já atualizada."""
    consulta = select(Convite).where(Convite.token_hash == hash_token(token), *condicoes_pendente())
    if travar:
        consulta = consulta.with_for_update()
    return db.scalar(consulta)
