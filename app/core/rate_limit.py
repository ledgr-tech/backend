"""Rate limiting (slowapi) em memória — suficiente pro MVP.

A chave padrão é o IP do cliente (`ip_do_cliente`). As rotas logadas limitam
pelo token: as da própria conta por usuário (`chave_por_usuario`), o upload
por conta (`chave_por_conta`, issue #133).

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
import logging
import math
import time

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.auth import ALGORITMO_JWT, PREFIXO_CHAVE_API, conta_id_do_claim, hash_da_chave_api
from app.core.config import settings

logger = logging.getLogger(__name__)

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

# Por conta (o `conta_id` do token, ou o legado `empresa_id`), não por IP: o
# upload chega pelo servidor do Next, com o IP da Vercel. Por conta, e não por
# empresa cliente, desde a issue #133: trocar de empresa não multiplica o limite.
LIMITE_UPLOAD = "10/minute"
# Download do arquivo original do extrato (issue #139, ADR-023): por conta e no
# mesmo valor do upload, pelo mesmo motivo.
LIMITE_DOWNLOAD_ORIGINAL = "10/minute"
# O /login, o /register e o /senha/* são chamados pelo servidor do Next: o
# limite por IP só separa os usuários quando o front repassa o IP do
# navegador (ver o docstring do módulo).
LIMITE_LOGIN = "10/minute"
LIMITE_CADASTRO = "5/minute"
# Consulta e aceite de convite (issue #69): públicas, recebem token e o aceite
# cria conta, então o mesmo limite do cadastro.
LIMITE_CONVITE = LIMITE_CADASTRO
# Verificação de e-mail e CNPJ antes do fim do cadastro (issue #80). A resposta
# diz qual dos dois já tem conta, então este limite é a proteção contra
# enumeração de e-mails e CNPJs cadastrados.
LIMITE_VERIFICACAO_CADASTRO = "10/minute"
# Recuperação de senha (issue #66). O limite que protege cada endereço é o de
# envios por hora, por destino, em app/api/senha.py.
LIMITE_RECUPERACAO_SENHA = "10/minute"
LIMITE_REDEFINICAO_SENHA = "10/minute"

# Troca de senha logado (issue #66): confere a senha atual, então serviria
# pra testar senhas com um token roubado. Por usuário, não por IP.
LIMITE_TROCA_SENHA_POR_USUARIO = "5/minute"
# Troca de e-mail (issue #149): o pedido por usuário, como as outras de /me;
# a confirmação é pública (o link pode abrir noutro navegador), por IP, como
# a redefinição de senha.
LIMITE_TROCA_EMAIL_POR_USUARIO = "5/minute"
LIMITE_CONFIRMACAO_TROCA_EMAIL = "10/minute"
# Desativação da própria conta (issue #68): também confere a senha atual.
LIMITE_DESATIVACAO_CONTA_POR_USUARIO = "5/minute"
# Transferência do admin (issue #133): também confere a senha atual.
LIMITE_TRANSFERENCIA_ADMIN_POR_USUARIO = "5/minute"
# Assistente da Central de ajuda (issue #137): cada pergunta chama o provedor.
LIMITE_ASSISTENTE_POR_USUARIO = "10/minute"
# API pública (issue #135): por chave, pelo sha256 do token, sem consultar o banco.
LIMITE_API_POR_CHAVE = "60/minute"
# Conexão MCP (issue #136): o registro de cliente e o authorize são públicos,
# por IP. O token não tem limite próprio: o ChatGPT e o Claude chegam de
# poucos IPs, e os segredos são aleatórios de 256 bits.
LIMITE_REGISTRO_OAUTH = "10/minute"
LIMITE_AUTORIZACAO_OAUTH = "30/minute"
# Servidor MCP (issue #136 parte 2): por conexão, pelo sha256 do token.
LIMITE_MCP_POR_CONEXAO = "60/minute"
# Aprovar e recusar a conexão, pela tela do front.
LIMITE_CONSENTIMENTO_MCP_POR_USUARIO = "10/minute"
# GET /me (issue #81): só leitura, sem risco de força bruta, mas o front
# consulta a cada carregamento de tela — limite bem mais alto que o da troca.
LIMITE_CONSULTA_ME_POR_USUARIO = "60/minute"
# PATCH /me/preferencias (issue #147): o front salva a cada tutorial dispensado.
LIMITE_PREFERENCIAS_POR_USUARIO = "30/minute"
# Favoritar e desfavoritar empresa (issue #157): um clique por empresa no seletor.
LIMITE_FAVORITAS_POR_USUARIO = "30/minute"
# Revisão da importação via IA (issue #159): corrigir, remover, adicionar e
# confirmar. A pessoa corrige linha a linha conferindo o PDF.
LIMITE_REVISAO_IMPORTACAO_POR_USUARIO = "60/minute"
# Link público do relatório (issue #165, ADR-024): criar e revogar, por
# usuário; a consulta pública, sem login, por IP (o do navegador, repassado
# pelo front com o segredo), contra a tentativa de adivinhar tokens.
LIMITE_LINKS_RELATORIO_POR_USUARIO = "10/minute"
LIMITE_RELATORIO_PUBLICO = "30/minute"
# Painel administrativo interno (issue #156, ADR-025): por operador (o usuário
# do token), leituras e ações em contadores separados.
LIMITE_ADMIN_LEITURA_POR_USUARIO = "60/minute"
LIMITE_ADMIN_ACAO_POR_USUARIO = "10/minute"


def _claims_do_token(request: Request) -> dict | None:
    """Os claims do Bearer, só quando a assinatura confere.

    O slowapi só confere o limite depois das dependencies da rota, então o
    token já foi validado quando as chaves abaixo rodam. A assinatura é
    conferida de novo assim mesmo: sem ela, um claim forjado esgotaria o
    limite de outra pessoa."""
    esquema, _, token = request.headers.get("authorization", "").partition(" ")
    if esquema.lower() != "bearer" or not token or not settings.nextauth_secret.strip():
        return None
    try:
        return jwt.decode(
            token,
            settings.nextauth_secret,
            algorithms=[ALGORITMO_JWT],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        return None


def chave_por_usuario(request: Request) -> str:
    """O `sub` do token, quando a assinatura confere; senão, o IP do cliente."""
    claims = _claims_do_token(request)
    sub = claims.get("sub") if claims else None
    return f"usuario:{sub}" if sub else ip_do_cliente(request)


def chave_por_conta(request: Request) -> str:
    """O id da conta do token (`conta_id` ou o legado `empresa_id`, pela mesma
    regra de app/core/auth.py), quando a assinatura confere; senão, o IP do
    cliente."""
    claims = _claims_do_token(request)
    conta_id = conta_id_do_claim(claims) if claims else None
    return f"conta:{conta_id}" if conta_id else ip_do_cliente(request)


def chave_por_chave_api(request: Request) -> str:
    """O sha256 do token da API pública (issue #135), sem consultar o banco;
    sem chave no formato, o IP do cliente. O slowapi só confere o limite
    depois das dependencies, então uma chave inválida já recebeu 401."""
    esquema, _, token = request.headers.get("authorization", "").partition(" ")
    if esquema.lower() != "bearer" or not token.startswith(PREFIXO_CHAVE_API):
        return ip_do_cliente(request)
    return f"chave_api:{hash_da_chave_api(token)}"


def limite_excedido(request: Request, exc: RateLimitExceeded) -> Response:
    """O 429 do slowapi com `Retry-After` em segundos, calculado pela janela do
    limite que estourou. Nas rotas `/v1` (issue #135), o corpo segue o formato
    da API (`detail` em texto)."""
    resposta = _rate_limit_exceeded_handler(request, exc)
    segundos = 60
    limite = getattr(request.state, "view_rate_limit", None)
    if limite is not None:
        try:
            reinicio, _ = limiter.limiter.get_window_stats(limite[0], *limite[1])
            segundos = max(1, math.ceil(reinicio - time.time()))
        except Exception as erro:  # noqa: BLE001 - sem a janela, vale o minuto inteiro
            logger.info("rate_limit janela_indisponivel classe=%s", type(erro).__name__)
    if request.url.path.startswith("/v1/"):
        resposta = JSONResponse(
            {"detail": "Limite de requisições excedido. Tente novamente em instantes."},
            status_code=429,
        )
    resposta.headers["Retry-After"] = str(segundos)
    return resposta
