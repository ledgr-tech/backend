"""Rate limiting (slowapi) em memória — suficiente pro MVP.

A chave padrão é o IP do cliente (`ip_do_cliente`). As rotas logadas limitam
pelo token: as da própria conta por usuário (`chave_por_usuario`), o upload
por empresa (`chave_por_empresa`).

O IP do cliente (issue #64) não é o da conexão: na Railway, toda requisição
chega pelo proxy dela, e o limite viraria um só para o produto inteiro. Vale,
nesta ordem:

1. O IP que o servidor do Next repassa em `X-Ledgr-IP-Cliente`, só quando
   `X-Ledgr-Segredo-Proxy` traz o `LEDGR_SEGREDO_PROXY`. O `/login`, o
   `/register` e o `/senha/*` são chamados pelo servidor da Vercel, então sem
   isso o IP que chega é o da Vercel, o mesmo para todos os usuários.
2. O último item do `X-Forwarded-For`, que é o que o proxy da Railway anotou.
   Os anteriores vêm do cliente e podem ser forjados (por isso
   `--forwarded-allow-ips="*"` no uvicorn não serve: ele usa o primeiro).
   Supõe exatamente um proxy na frente.
3. O IP da conexão (rodando local, sem proxy).
"""

import hmac
import ipaddress

import jwt
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.auth import ALGORITMO_JWT
from app.core.config import settings

CABECALHO_IP_CLIENTE = "X-Ledgr-IP-Cliente"
CABECALHO_SEGREDO_PROXY = "X-Ledgr-Segredo-Proxy"


def _ip_valido(valor: str) -> str | None:
    try:
        return str(ipaddress.ip_address(valor.strip()))
    except ValueError:
        return None


def _ip_repassado_pelo_front(request: Request) -> str | None:
    segredo = settings.ledgr_segredo_proxy.strip()
    recebido = request.headers.get(CABECALHO_SEGREDO_PROXY, "")
    # sem segredo no backend, nada é confiável: um segredo vazio não pode bater
    if not segredo or not hmac.compare_digest(recebido.encode(), segredo.encode()):
        return None
    return _ip_valido(request.headers.get(CABECALHO_IP_CLIENTE, ""))


def ip_do_cliente(request: Request) -> str:
    ip = _ip_repassado_pelo_front(request)
    if ip is None:
        encaminhado = request.headers.get("x-forwarded-for", "")
        if encaminhado:
            ip = _ip_valido(encaminhado.split(",")[-1])
    return f"ip:{ip or get_remote_address(request)}"


limiter = Limiter(key_func=ip_do_cliente)

# Por empresa (o `empresa_id` do token), não por IP: o upload chega pelo
# servidor do Next, com o IP da Vercel.
LIMITE_UPLOAD = "10/minute"
# O /login, o /register e o /senha/* são chamados pelo servidor do Next: o
# limite por IP só separa os usuários quando o front repassa o IP do
# navegador (ver o docstring do módulo).
LIMITE_LOGIN = "10/minute"
LIMITE_CADASTRO = "5/minute"
# Recuperação de senha (issue #66). O limite que protege cada endereço é o de
# envios por hora, por destino, em app/api/senha.py.
LIMITE_RECUPERACAO_SENHA = "10/minute"
LIMITE_REDEFINICAO_SENHA = "10/minute"

# Troca de senha logado (issue #66): confere a senha atual, então serviria
# pra testar senhas com um token roubado. Por usuário, não por IP.
LIMITE_TROCA_SENHA_POR_USUARIO = "5/minute"
# GET /me (issue #81): só leitura, sem risco de força bruta, mas o front
# consulta a cada carregamento de tela — limite bem mais alto que o da troca.
LIMITE_CONSULTA_ME_POR_USUARIO = "60/minute"


def _claim_do_token(request: Request, nome: str) -> str | None:
    """O claim do Bearer, só quando a assinatura confere.

    O slowapi só confere o limite depois das dependencies da rota, então o
    token já foi validado quando as chaves abaixo rodam. A assinatura é
    conferida de novo assim mesmo: sem ela, um claim forjado esgotaria o
    limite de outra pessoa."""
    esquema, _, token = request.headers.get("authorization", "").partition(" ")
    if esquema.lower() != "bearer" or not token or not settings.nextauth_secret.strip():
        return None
    try:
        claims = jwt.decode(
            token,
            settings.nextauth_secret,
            algorithms=[ALGORITMO_JWT],
            options={"require": ["exp", nome]},
        )
    except jwt.PyJWTError:
        return None
    return str(claims[nome])


def chave_por_usuario(request: Request) -> str:
    """O `sub` do token, quando a assinatura confere; senão, o IP do cliente."""
    sub = _claim_do_token(request, "sub")
    return f"usuario:{sub}" if sub else ip_do_cliente(request)


def chave_por_empresa(request: Request) -> str:
    """O `empresa_id` do token, quando a assinatura confere; senão, o IP do
    cliente."""
    empresa_id = _claim_do_token(request, "empresa_id")
    return f"empresa:{empresa_id}" if empresa_id else ip_do_cliente(request)
