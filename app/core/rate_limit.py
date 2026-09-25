"""Rate limiting (slowapi) por IP, em memória — suficiente pro MVP."""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def ip_do_cliente(request: Request) -> str:
    # Na Railway, a conexão vem sempre do proxy dela, então request.client é o
    # IP do proxy e o limite viraria um só pra todo mundo. O último item do
    # X-Forwarded-For é o que o proxy anotou; os anteriores vêm do cliente e
    # podem ser forjados. Supõe exatamente um proxy na frente (a Railway).
    encaminhado = request.headers.get("x-forwarded-for")
    if encaminhado:
        ultimo = encaminhado.split(",")[-1].strip()
        if ultimo:
            return ultimo
    return get_remote_address(request)


limiter = Limiter(key_func=ip_do_cliente)

LIMITE_UPLOAD = "10/minute"
