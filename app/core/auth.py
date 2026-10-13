"""Autenticação por JWT (ADR-003) — interface entre backend e frontend.

Contrato do token, emitido pelo NextAuth no frontend (outro repo):
- Algoritmo HS256, assinado com NEXTAUTH_SECRET (segredo compartilhado).
- Claims: `sub` (usuario_id, string), `empresa_id` (string UUID), `email`,
  `iat`, `exp` e, desde a issue #75, `auth_time`: o momento do login, em
  segundos inteiros desde a época, gravado só no login e mantido em cada
  renovação da sessão.
- Enviado em `Authorization: Bearer <token>`.

Toda rota autenticada passa por `_usuario_do_token` (issue #75, ADR-016),
que exige `exp` e `auth_time`, carrega o usuário do `sub` por chave primária
e só deixa passar quando:

- o usuário existe, não foi removido (issue #69) e é da empresa do token;
- `auth_time` não está no futuro nem tem mais de `DURACAO_SESSAO_SEGUNDOS`:
  a sessão dura 7 dias desde o login (ADR-003), mesmo que o front renove o
  token com `iat` e `exp` novos;
- `auth_time` não é anterior a `usuarios.sessoes_validas_desde`, que
  `encerrar_sessoes` grava na troca de senha, na redefinição pelo link e na
  remoção do usuário.

Qualquer falha vira o mesmo 401, sem detalhar o motivo. A empresa continua
vindo do token; a consulta confere o usuário, e foi o preço aceito para
revogar a sessão (antes, as rotas que só liam o `empresa_id` não consultavam
o banco). As chaves do rate limit (app/core/rate_limit.py) continuam sem
banco.

`obter_empresa_id_autenticada` devolve o `empresa_id`; `obter_usuario_autenticado`
devolve o usuário, para as rotas da própria conta (`/me/*`, issue #66) e as
que registram quem fez o quê. As duas usam a mesma função, para nunca
divergirem.

`obter_administrador_autenticado` (issue #69, ADR-015) é a mesma coisa, mas
só deixa passar o administrador da empresa; os demais recebem 403. Não há
papéis: o administrador é quem cadastrou a empresa.
"""

import math
import uuid
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi import status as http_status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models import Usuario

ALGORITMO_JWT = "HS256"

# ADR-003: a sessão vale 7 dias desde o login, contados do `auth_time`.
# O front usa o mesmo número (`DURACAO_SESSAO_SEGUNDOS` em lib/token.ts).
DURACAO_SESSAO_SEGUNDOS = 7 * 24 * 60 * 60

_bearer = HTTPBearer(auto_error=False)


def normalizar_email(email: str) -> str:
    """Forma única do e-mail: a que fica no banco e a chave do bloqueio de login.

    Mora aqui, e não em app/api/auth.py, para app/core/bloqueio_login.py usar
    sem importar a camada de rotas (issue #67)."""
    return email.strip().lower()


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
            options={"require": ["exp", "auth_time"]},
        )
    except jwt.PyJWTError as exc:
        raise _nao_autenticado() from exc


def _uuid_do_claim(claims: dict, nome: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(claims[nome]))
    except (KeyError, ValueError) as exc:
        raise _nao_autenticado() from exc


def _auth_time(claims: dict) -> int:
    """O `auth_time` do token, só se for inteiro e não estiver no futuro.

    Sem folga para relógio adiantado, a mesma regra que o PyJWT aplica ao
    `iat`, que nunca é anterior ao `auth_time`."""
    valor = claims["auth_time"]
    # bool é subclasse de int no Python, e `true` não é um instante
    if type(valor) is not int or valor > datetime.now(UTC).timestamp():
        raise _nao_autenticado()
    return valor


def _sessao_valida(usuario: Usuario, auth_time: int) -> bool:
    if datetime.now(UTC).timestamp() - auth_time > DURACAO_SESSAO_SEGUNDOS:
        return False
    if usuario.sessoes_validas_desde is None:
        return True
    return auth_time >= math.floor(usuario.sessoes_validas_desde.timestamp())


def _usuario_do_token(credenciais: HTTPAuthorizationCredentials | None, db: Session) -> Usuario:
    claims = _claims_do_token(credenciais)
    usuario_id = _uuid_do_claim(claims, "sub")
    empresa_id = _uuid_do_claim(claims, "empresa_id")
    auth_time = _auth_time(claims)
    usuario = db.get(Usuario, usuario_id)
    if (
        usuario is None
        or usuario.removido_em is not None
        or usuario.empresa_id != empresa_id
        or not _sessao_valida(usuario, auth_time)
    ):
        raise _nao_autenticado()
    return usuario


def encerrar_sessoes(usuario: Usuario) -> None:
    """Invalida todo token emitido antes de agora, inclusive o de quem chamou.

    Truncado no segundo porque o `auth_time` é inteiro: sem isso, o login
    feito logo depois, ainda no mesmo segundo, sairia com `auth_time`
    anterior ao valor gravado e daria 401. Sem commit: vale com a transação
    de quem chama."""
    usuario.sessoes_validas_desde = func.date_trunc("second", func.now())


def obter_empresa_id_autenticada(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> uuid.UUID:
    return _usuario_do_token(credenciais, db).empresa_id


def obter_usuario_autenticado(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Usuario:
    return _usuario_do_token(credenciais, db)


def obter_administrador_autenticado(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
) -> Usuario:
    if not usuario.administrador:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Só o administrador da empresa pode fazer isso.",
        )
    return usuario
