"""Rate limiting (slowapi) por IP, em memória — suficiente pro MVP."""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

LIMITE_UPLOAD = "10/minute"
# O /login é chamado pelo servidor do NextAuth, não pelo navegador: o IP que
# chega aqui é o da Vercel, então o limite vale pra todos os usuários juntos.
LIMITE_LOGIN = "10/minute"
LIMITE_CADASTRO = "5/minute"
