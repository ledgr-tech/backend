"""Autenticação stateless por JWT (ADR-003) — interface entre backend e frontend.

Contrato do token, emitido pelo NextAuth no frontend (outro repo):
- Algoritmo HS256, assinado com NEXTAUTH_SECRET (segredo compartilhado).
- Claims: `sub` (usuario_id, string), `empresa_id` (string UUID), `email`,
  `iat` e `exp` (validade de 7 dias, ADR-003).
- Enviado em `Authorization: Bearer <token>`.

O backend só lê `empresa_id` (e exige `exp`, pra token sem validade não valer
pra sempre). Não consulta o banco pra resolver a empresa: é o ponto central
da ADR-003. Qualquer falha vira 401 sem detalhar o motivo.
"""

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi import status as http_status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

ALGORITMO_JWT = "HS256"

_bearer = HTTPBearer(auto_error=False)


def _nao_autenticado() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def obter_empresa_id_autenticada(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    if credenciais is None:
        raise _nao_autenticado()

    secret = settings.exigir_nextauth_secret()
    try:
        claims = jwt.decode(
            credenciais.credentials,
            secret,
            algorithms=[ALGORITMO_JWT],
            options={"require": ["exp"]},
        )
        return uuid.UUID(str(claims["empresa_id"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise _nao_autenticado() from exc
