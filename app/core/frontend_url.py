"""Decide se um FRONTEND_URL serve pra montar link de e-mail (issue #79).

O problema que isto resolve: FRONTEND_URL estava apontando pra uma URL de
deploy da Vercel (`frontend-hfrnofw9w-ledgr7.vercel.app`). Cada deploy do front
gera uma URL nova e aposenta a anterior, então todo link de recuperação de senha
já enviado parava de funcionar no deploy seguinte. O valor certo é o domínio
estável de produção, `https://ledgrfinance.com.br`.

`avaliar_frontend_url` é pura: recebe o valor, devolve a URL normalizada ou o
motivo da recusa. Quem lê a configuração e loga é `frontend_url_para_link`.

O que serve é esquema, host e, se quiser, caminho — nada além disso. Query
string e fragmento são recusados porque a base é concatenada com o caminho e o
fragmento do link (`/redefinir-senha#token=...`): um `#` na base produziria um
link com dois fragmentos, e uma query ficaria no meio do caminho.

URL recusada se comporta igual a URL vazia — envio indisponível, nunca um link
quebrado, que é a regra que já estava em app/core/config.py. A diferença é o
log: valor vazio é o estado documentado de "e-mail desligado" (o default de
`frontend_url`), então não vira ERROR; valor preenchido e recusado vira, porque
aí alguém configurou errado e precisa ver isso no log do Railway.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import settings

logger = logging.getLogger(__name__)

# http só pra desenvolvimento, e só nestes hosts (com ou sem porta).
HOSTS_HTTP_PERMITIDOS = frozenset({"localhost", "127.0.0.1"})

SUFIXO_VERCEL = ".vercel.app"
# Alias de branch da Vercel: `frontend-git-main-ledgr7.vercel.app`.
MARCA_ALIAS_DE_BRANCH = "-git-"
# Identificador de deploy da Vercel: 9 caracteres [a-z0-9] num segmento próprio
# do host, como o `hfrnofw9w` de `frontend-hfrnofw9w-ledgr7.vercel.app`. O alias
# estável de produção (`frontend-five-azure-50.vercel.app`) não tem segmento
# nesse formato, e é por isso que dá pra separar um do outro só pelo nome.
IDENTIFICADOR_DE_DEPLOY = re.compile(r"^[a-z0-9]{9}$")

MOTIVO_VAZIO = "vazio"
MOTIVO_URL_INVALIDA = "url_invalida"
MOTIVO_SEM_ESQUEMA = "sem_esquema"
MOTIVO_ESQUEMA_NAO_HTTPS = "esquema_nao_https"
MOTIVO_DEPLOY_VERCEL = "deploy_especifico_vercel"
MOTIVO_QUERY_OU_FRAGMENTO = "query_ou_fragmento_nao_permitido"


@dataclass(frozen=True)
class FrontendUrlAvaliada:
    """`url` preenchida quando serve; `motivo`/`detalhe` quando não.

    `motivo` é código curto, pro log em `chave=valor` que o resto do projeto já
    usa; `detalhe` é a frase pra quem for ler esse log.
    """

    url: str | None = None
    motivo: str | None = None
    detalhe: str | None = None

    @property
    def serve(self) -> bool:
        return self.url is not None


def _e_deploy_especifico_da_vercel(host: str) -> bool:
    """Host de deploy individual da Vercel, que morre no deploy seguinte."""
    if not host.endswith(SUFIXO_VERCEL):
        return False
    prefixo = host[: -len(SUFIXO_VERCEL)]
    if MARCA_ALIAS_DE_BRANCH in prefixo:
        return True
    segmentos = prefixo.split("-")
    # Só os segmentos do meio: o primeiro é o nome do projeto e o último é o
    # escopo/time, e qualquer um dos dois pode ter 9 caracteres por coincidência.
    return any(IDENTIFICADOR_DE_DEPLOY.match(segmento) for segmento in segmentos[1:-1])


def avaliar_frontend_url(valor: str) -> FrontendUrlAvaliada:
    """Avalia `valor` como base de link de e-mail. Função pura, sem I/O.

    Aceita e devolve normalizada (sem espaço nas pontas e sem `/` no fim):

    - `https://` com host, de qualquer domínio que não seja deploy da Vercel;
    - `http://localhost` e `http://127.0.0.1`, com ou sem porta, pra
      desenvolvimento.

    Caminho é aceito (`https://exemplo.com.br/app`), query string e fragmento
    não.

    Recusa valor vazio, URL sem esquema ou com esquema que não seja esses, host
    `*.vercel.app` que tenha cara de deploy específico, e URL com `?` ou `#`.
    `url_invalida` cobre o que não dá pra interpretar (URL que o parser recusa,
    ou `https://` sem host): a função nunca levanta, porque roda dentro de
    request.
    """
    url = (valor or "").strip()
    if not url:
        return FrontendUrlAvaliada(motivo=MOTIVO_VAZIO, detalhe="FRONTEND_URL está vazio.")

    try:
        partes = urlsplit(url)
        esquema, host, _porta = partes.scheme, partes.hostname, partes.port
    except ValueError as erro:
        return FrontendUrlAvaliada(
            motivo=MOTIVO_URL_INVALIDA,
            detalhe=f"FRONTEND_URL {url!r} não é uma URL interpretável ({erro}).",
        )

    if not esquema:
        return FrontendUrlAvaliada(
            motivo=MOTIVO_SEM_ESQUEMA,
            detalhe=(
                f"FRONTEND_URL {url!r} não tem esquema. Precisa começar com "
                f"https:// (o domínio estável de produção)."
            ),
        )
    if not (esquema == "https" or (esquema == "http" and host in HOSTS_HTTP_PERMITIDOS)):
        return FrontendUrlAvaliada(
            motivo=MOTIVO_ESQUEMA_NAO_HTTPS,
            detalhe=(
                f"FRONTEND_URL {url!r} usa esquema {esquema!r}. Só https:// serve, "
                f"e http:// apenas em {sorted(HOSTS_HTTP_PERMITIDOS)} (desenvolvimento)."
            ),
        )
    if not host:
        return FrontendUrlAvaliada(
            motivo=MOTIVO_URL_INVALIDA,
            detalhe=f"FRONTEND_URL {url!r} não tem host.",
        )
    if _e_deploy_especifico_da_vercel(host):
        return FrontendUrlAvaliada(
            motivo=MOTIVO_DEPLOY_VERCEL,
            detalhe=(
                f"FRONTEND_URL {url!r} é um deploy específico da Vercel: a URL muda "
                f"no deploy seguinte do front e todo link já enviado quebra. Use o "
                f"domínio estável de produção."
            ),
        )
    # `?` e `#` só existem numa URL como delimitador de query e de fragmento,
    # então procurar o caractere cru pega também o caso degenerado
    # (`https://exemplo.com.br?`), que o parser devolveria como query vazia.
    if "?" in url or "#" in url:
        return FrontendUrlAvaliada(
            motivo=MOTIVO_QUERY_OU_FRAGMENTO,
            detalhe=(
                f"FRONTEND_URL {url!r} tem query string ou fragmento. A base é "
                f"concatenada com o caminho e o fragmento do link "
                f"(`/redefinir-senha#token=...`), então sobraria um link com dois "
                f"`#` ou com a query no meio do caminho. Use só esquema, host e "
                f"caminho."
            ),
        )
    return FrontendUrlAvaliada(url=url.rstrip("/"))


def _logar_recusa(avaliada: FrontendUrlAvaliada, origem: str) -> None:
    """ERROR só pra valor preenchido e recusado (ver docstring do módulo).

    Loga o valor configurado e o motivo, nada mais: aqui não passa token, link
    nem endereço de e-mail.
    """
    if avaliada.motivo == MOTIVO_VAZIO:
        return
    logger.error(
        "frontend_url_recusada origem=%s motivo=%s detalhe=%s",
        origem,
        avaliada.motivo,
        avaliada.detalhe,
    )


def frontend_url_para_link() -> str | None:
    """Base pronta pra montar link de e-mail, ou None se a configuração não serve."""
    avaliada = avaliar_frontend_url(settings.frontend_url)
    if avaliada.serve:
        return avaliada.url
    _logar_recusa(avaliada, "requisicao")
    return None


def avisar_frontend_url_no_startup() -> None:
    """Chamada no startup (main.py) pra recusa aparecer no log de deploy.

    Sem isto, uma FRONTEND_URL errada só apareceria no log quando alguém
    pedisse recuperação de senha — possivelmente dias depois do deploy.
    """
    avaliada = avaliar_frontend_url(settings.frontend_url)
    if avaliada.serve:
        return
    _logar_recusa(avaliada, "startup")
