"""Rate limiting (slowapi) em memória — suficiente pro MVP. Por IP, menos
nas rotas da própria conta, que limitam por usuário (`chave_por_usuario`)."""

import jwt
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.auth import ALGORITMO_JWT
from app.core.config import settings

limiter = Limiter(key_func=get_remote_address)

LIMITE_UPLOAD = "10/minute"
# O /login é chamado pelo servidor do NextAuth, não pelo navegador: o IP que
# chega aqui é o da Vercel, então o limite vale pra todos os usuários juntos.
LIMITE_LOGIN = "10/minute"
LIMITE_CADASTRO = "5/minute"
# Recuperação de senha (issue #66): também chega pelo servidor do Next, com
# o IP da Vercel. O limite que protege cada endereço é o de envios por hora,
# por destino, em app/api/senha.py.
LIMITE_RECUPERACAO_SENHA = "10/minute"
LIMITE_REDEFINICAO_SENHA = "10/minute"

# Troca de senha logado (issue #66): confere a senha atual, então serviria
# pra testar senhas com um token roubado. Por usuário, não por IP: o IP que
# chega é o da Vercel, o mesmo pra todos.
LIMITE_TROCA_SENHA_POR_USUARIO = "5/minute"


def chave_por_usuario(request: Request) -> str:
    """O `sub` do token, quando a assinatura confere; senão, o IP.

    O slowapi só confere o limite depois das dependencies da rota, então nas
    rotas `/me/*` o token já foi validado quando esta função roda. A
    assinatura é conferida de novo assim mesmo: sem ela, um `sub` forjado
    esgotaria o limite de outra pessoa."""
    esquema, _, token = request.headers.get("authorization", "").partition(" ")
    if esquema.lower() == "bearer" and token and settings.nextauth_secret.strip():
        try:
            claims = jwt.decode(
                token,
                settings.nextauth_secret,
                algorithms=[ALGORITMO_JWT],
                options={"require": ["exp", "sub"]},
            )
            return f"usuario:{claims['sub']}"
        except jwt.PyJWTError:
            pass
    return get_remote_address(request)
