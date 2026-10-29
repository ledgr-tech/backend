"""Os redirect_uris aceitos no registro de cliente OAuth (issue #136, ADR-022).

Só o ChatGPT, o Claude e o loopback do Claude Code. Um cliente qualquer não
consegue registrar o próprio endereço e usar a tela de consentimento do Ledgr
para roubar códigos de autorização.

Cada item da lista é uma URL. `*` num segmento do caminho vale um segmento
qualquer (o id do callback do ChatGPT). Em `http://localhost` e
`http://127.0.0.1` (loopback, RFC 8252), a porta não conta: o Claude Code usa
uma porta nova a cada sessão. A mesma regra vale na comparação do authorize
com os redirect_uris registrados (`mesmo_redirect`).
"""

import re
from urllib.parse import urlsplit

from app.core.config import settings

REDIRECTS_PERMITIDOS_PADRAO = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chatgpt.com/connector/oauth/*",
    "http://localhost/*",
    "http://127.0.0.1/*",
)
HOSTS_LOOPBACK = frozenset({"localhost", "127.0.0.1"})
_SEGMENTO = re.compile(r"^[A-Za-z0-9._~-]+$")


def _partes(uri: str):
    try:
        partes = urlsplit(uri)
        partes.port  # noqa: B018 - porta inválida levanta ValueError
    except ValueError:
        return None
    if not partes.scheme or not partes.hostname or partes.fragment:
        return None
    return partes


def eh_loopback(uri: str) -> bool:
    partes = _partes(uri)
    return partes is not None and partes.scheme == "http" and partes.hostname in HOSTS_LOOPBACK


def _sem_porta_se_loopback(uri: str) -> tuple | None:
    partes = _partes(uri)
    if partes is None:
        return None
    loopback = partes.scheme == "http" and partes.hostname in HOSTS_LOOPBACK
    porta = None if loopback else partes.port
    return (partes.scheme, partes.hostname, porta, partes.path or "/", partes.query)


def redirects_permitidos() -> tuple[str, ...]:
    configurados = tuple(
        item.strip() for item in settings.oauth_redirects_permitidos.split(",") if item.strip()
    )
    return configurados or REDIRECTS_PERMITIDOS_PADRAO


def _bate_com_o_modelo(uri: str, modelo: str) -> bool:
    candidato, padrao = _sem_porta_se_loopback(uri), _sem_porta_se_loopback(modelo)
    if candidato is None or padrao is None or candidato[4] or padrao[4]:
        return False
    if candidato[:3] != padrao[:3]:
        return False
    caminho, caminho_modelo = candidato[3].split("/"), padrao[3].split("/")
    if len(caminho) != len(caminho_modelo):
        return False
    return all(
        (esperado == "*" and _SEGMENTO.match(parte)) or parte == esperado
        for parte, esperado in zip(caminho, caminho_modelo, strict=True)
    )


def redirect_permitido(uri: str) -> bool:
    """O redirect_uri pode ser registrado? Sem query string nem fragmento."""
    return any(_bate_com_o_modelo(uri, modelo) for modelo in redirects_permitidos())


def mesmo_redirect(pedido: str, registrado: str) -> bool:
    """O redirect_uri do authorize é o registrado? Igual, a não ser a porta no
    loopback."""
    if pedido == registrado:
        return True
    return (
        eh_loopback(pedido)
        and eh_loopback(registrado)
        and _sem_porta_se_loopback(pedido) == _sem_porta_se_loopback(registrado)
    )
