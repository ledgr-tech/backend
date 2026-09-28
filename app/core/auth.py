"""Autenticação stateless por JWT (ADR-003) — interface entre backend e frontend.

Contrato do token, emitido pelo NextAuth no frontend (outro repo):
- Algoritmo HS256, assinado com NEXTAUTH_SECRET (segredo compartilhado).
- Claims: `sub` (usuario_id, string), `empresa_id` (string UUID), `email`,
  `iat` e `exp` (validade de 7 dias, ADR-003).
- Enviado em `Authorization: Bearer <token>`.

A maioria das rotas só lê `empresa_id` (e exige `exp`, pra token sem validade
não valer pra sempre). Não consulta o banco pra resolver a empresa: é o ponto
central da ADR-003. Qualquer falha vira 401 sem detalhar o motivo.

As rotas da própria conta (`/me/*`, issue #66) precisam saber qual usuário
está chamando: `obter_usuario_autenticado` lê também o `sub` e carrega o
usuário com esse `id` E o `empresa_id` do token. Aqui a consulta ao banco é
inerente (a rota precisa do hash da senha), o que o ADR-005 não rejeita.
Usuário não encontrado também é 401.
"""

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi import status as http_status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models import Usuario

ALGORITMO_JWT = "HS256"

_bearer = HTTPBearer(auto_error=False)


def _nao_autenticado() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _claims_do_token(credenciais: HTTPAuthorizationCredentials | None) -> dict:
    if credenciais is None:
        raise _nao_autenticado()

    secret = settings.exigir_nextauth_secret()
    try:
        return jwt.decode(
            credenciais.credentials,
            secret,
            algorithms=[ALGORITMO_JWT],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise _nao_autenticado() from exc


def _uuid_do_claim(claims: dict, nome: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(claims[nome]))
    except (KeyError, ValueError) as exc:
        raise _nao_autenticado() from exc


def obter_empresa_id_autenticada(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    return _uuid_do_claim(_claims_do_token(credenciais), "empresa_id")


def obter_usuario_autenticado(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Usuario:
    claims = _claims_do_token(credenciais)
    usuario_id = _uuid_do_claim(claims, "sub")
    empresa_id = _uuid_do_claim(claims, "empresa_id")
    usuario = db.scalar(
        select(Usuario).where(Usuario.id == usuario_id, Usuario.empresa_id == empresa_id)
    )
    if usuario is None:
        raise _nao_autenticado()
    return usuario
