"""Servidor MCP remoto do Ledgr (issue #136, parte 2; ADR-022).

O ChatGPT e o Claude do cliente conectam aqui, com o token de acesso emitido
pelo servidor de autorização do próprio Ledgr (parte 1, app/api/oauth.py).
SDK oficial do MCP (`mcp`, `MCPServer`, o antigo FastMCP), transporte
Streamable HTTP sem estado e com respostas JSON, no caminho de
`MCP_URL_PUBLICA` (ex.: /mcp), exatamente nele: a rota é exata, sem o
redirecionamento para a barra no fim que um `Mount` faria.

Antes de qualquer coisa do SDK, `PortaDoMcp`, nesta ordem:

- flag desligada: 404; ligada sem as URLs: 503;
- `Origin` presente e fora da lista (a origem do próprio servidor, claude.ai e
  chatgpt.com): 403, como a especificação manda contra DNS rebinding;
- token: a mesma regra da dependency `obter_conexao_mcp`
  (`autenticar_token_mcp`, app/core/auth.py). Sem token ou com token
  inválido, vencido, revogado ou de outro resource, 401 com o
  `WWW-Authenticate` que manda o cliente à descoberta OAuth (o Claude só
  começa o login com 401). O JWT do app e a chave de API nunca valem aqui;
- rate limit de `LIMITE_MCP_POR_CONEXAO` pelo sha256 do token, sem banco:
  429 com `Retry-After`.

A conexão vai para as ferramentas em `scope["state"]`, que o SDK expõe em
`ctx.request_context.request.state`. As ferramentas são só de leitura,
sempre da empresa da conexão, e usam a mesma regra da `/v1`
(app/services/api_publica.py): nunca devolvem descrição de lançamento, texto
de justificativa, ressalva, nome de pessoa ou nome de arquivo. Cada uma
devolve o resultado estruturado (com `outputSchema`) e um texto curto em
português; entrada inválida é erro da ferramenta (`isError`), nunca exceção
HTTP.

Logs: o nome da ferramenta, o id da conexão e o resultado. Nunca o token nem o
conteúdo das respostas.
"""

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

import anyio
from fastapi import HTTPException
from limits import parse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from app.core.auth import (
    PREFIXO_TOKEN_MCP,
    ConexaoAutenticada,
    autenticar_token_mcp,
    registrar_uso_da_conexao,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.rate_limit import LIMITE_MCP_POR_CONEXAO, limiter
from app.models import Empresa
from app.services import api_publica
from app.services.api_publica import (
    CATEGORIAS_DE_DIVERGENCIA,
    LIMITE_MAXIMO,
    LIMITE_PADRAO,
    PADRAO_COMPETENCIA,
    SITUACOES,
    ConciliacoesV1,
    DivergenciasV1,
    FechamentosV1,
    MesesV1,
    ResumoV1,
)

logger = logging.getLogger(__name__)

CAMINHO_PADRAO = "/mcp"
ORIGENS_DOS_CLIENTES = ("https://claude.ai", "https://chatgpt.com")
MESES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
INSTRUCOES = (
    "Ledgr é um sistema brasileiro de conciliação bancária. Estas ferramentas leem, só para "
    "consulta, a situação das conciliações de UMA empresa: a que o usuário escolheu ao "
    "conectar. Comece por empresa_conectada ou meses_com_conciliacao. Competência é o mês no "
    "formato AAAA-MM (ex.: 2026-09). Valores vêm como texto decimal em reais, com duas casas "
    "e ponto (negativo é saída); datas em ISO 8601. As ferramentas nunca devolvem descrição "
    "de lançamento, texto de justificativa, texto de ressalva, nome de arquivo nem nome de "
    "pessoa: não invente esses dados."
)
SO_LEITURA = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def caminho_do_mcp() -> str:
    """O caminho de `MCP_URL_PUBLICA` (ex.: /mcp), lido no boot."""
    caminho = urlsplit(settings.mcp_url_publica.strip()).path.rstrip("/")
    return caminho or CAMINHO_PADRAO


# --- textos --------------------------------------------------------------------------------


def _mes_por_extenso(competencia: str) -> str:
    ano, mes = competencia.split("-")
    return f"{MESES[int(mes) - 1].capitalize()} de {ano}"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _cnpj_formatado(cnpj: str) -> str:
    if len(cnpj) != 14:
        return cnpj
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


# --- conexão, erros e resultado ------------------------------------------------------------


def _conexao(ctx: Context) -> ConexaoAutenticada:
    return ctx.request_context.request.state.conexao_mcp


def _erro(nome: str, conexao: ConexaoAutenticada, mensagem: str, resultado: str) -> CallToolResult:
    logger.info(
        "mcp_ferramenta nome=%s conexao_id=%s resultado=%s", nome, conexao.conexao_id, resultado
    )
    return CallToolResult(content=[TextContent(type="text", text=mensagem)], is_error=True)


def _sucesso(
    nome: str, conexao: ConexaoAutenticada, modelo: BaseModel, texto: str
) -> CallToolResult:
    logger.info("mcp_ferramenta nome=%s conexao_id=%s resultado=ok", nome, conexao.conexao_id)
    return CallToolResult(
        content=[TextContent(type="text", text=texto)],
        structured_content=modelo.model_dump(mode="json"),
    )


def _competencia_invalida(competencia: str | None) -> bool:
    return competencia is None or not re.fullmatch(PADRAO_COMPETENCIA, competencia)


ERRO_COMPETENCIA = "Competência inválida: use o formato AAAA-MM, com mês de 01 a 12 (ex.: 2026-09)."


# --- ferramentas ---------------------------------------------------------------------------


def _sem_mexer_no_logger_raiz(criar):
    """O `MCPServer` chama `logging.basicConfig`, que põe um handler no logger
    raiz. Os logs do processo são configurados só em app/core/logging.py
    (issue #102): o handler e o nível do raiz voltam a ser os de antes."""
    raiz = logging.getLogger()
    handlers, nivel = list(raiz.handlers), raiz.level
    try:
        return criar()
    finally:
        for handler in list(raiz.handlers):
            if handler not in handlers:
                raiz.removeHandler(handler)
        raiz.setLevel(nivel)


servidor = _sem_mexer_no_logger_raiz(
    lambda: MCPServer(name="ledgr", title="Ledgr", instructions=INSTRUCOES, version="1")
)

ParametroCompetencia = Annotated[
    str,
    Field(description="Mês de referência no formato AAAA-MM, por exemplo 2026-09."),
]


class EmpresaConectada(BaseModel):
    """A empresa que a conexão lê."""

    razao_social: str = Field(description="Razão social da empresa.")
    cnpj: str = Field(description="CNPJ da empresa, sem máscara (14 caracteres).")


@servidor.tool(
    title="Empresa conectada",
    description=(
        "Devolve a razão social e o CNPJ da empresa que esta conexão lê. A conexão vale para "
        "uma empresa só, escolhida pelo usuário ao conectar: todas as outras ferramentas "
        "respondem sobre ela. Use para confirmar de qual empresa se trata."
    ),
    annotations=SO_LEITURA,
)
def empresa_conectada(ctx: Context) -> Annotated[CallToolResult, EmpresaConectada]:
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        empresa = db.get(Empresa, conexao.empresa_id)
        modelo = EmpresaConectada(razao_social=empresa.razao_social, cnpj=empresa.cnpj)
    texto = f"Empresa conectada: {modelo.razao_social} (CNPJ {_cnpj_formatado(modelo.cnpj)})."
    return _sucesso("empresa_conectada", conexao, modelo, texto)


@servidor.tool(
    title="Meses com conciliação",
    description=(
        "Lista as competências (meses, AAAA-MM) que têm conciliação, das mais recentes para as "
        "mais antigas, no máximo 24, cada uma com `fechado` (se o mês está fechado). A "
        "competência de uma conciliação é o mês em que começa o extrato do banco. Use para "
        "descobrir quais meses consultar antes de chamar as outras ferramentas."
    ),
    annotations=SO_LEITURA,
)
def meses_com_conciliacao(ctx: Context) -> Annotated[CallToolResult, MesesV1]:
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        modelo = api_publica.meses_com_conciliacao(db, conexao.empresa_id)
    if not modelo.itens:
        texto = "Nenhum mês com conciliação nesta empresa."
    else:
        recente = modelo.itens[0]
        texto = (
            f"{_plural(len(modelo.itens), 'mês', 'meses')} com conciliação; o mais recente é "
            f"{_mes_por_extenso(recente.competencia).lower()} "
            f"({'fechado' if recente.fechado else 'aberto'})."
        )
    return _sucesso("meses_com_conciliacao", conexao, modelo, texto)


@servidor.tool(
    title="Resumo do mês",
    description=(
        "Situação de uma competência: se o mês está fechado (e se foi com ressalva, sem o texto "
        "dela), se está pronto para fechar e quantas linhas há em cada categoria da conciliação "
        "e em cada situação (justificadas, aguardando aprovação, pendentes, linhas não lidas). "
        "Use para responder 'como está setembro?' ou 'quantas divergências tive?'. "
        "Divergências são as linhas fora de match_exato e match_tolerancia."
    ),
    annotations=SO_LEITURA,
)
def resumo_do_mes(
    competencia: ParametroCompetencia, ctx: Context
) -> Annotated[CallToolResult, ResumoV1]:
    conexao = _conexao(ctx)
    if _competencia_invalida(competencia):
        return _erro("resumo_do_mes", conexao, ERRO_COMPETENCIA, "competencia_invalida")
    with SessionLocal() as db:
        modelo = api_publica.resumo_do_mes(db, conexao.empresa_id, competencia)
    c = modelo.contagens
    divergencias = c.total - c.match_exato - c.match_tolerancia
    if modelo.fechado:
        estado = "mês fechado com ressalva" if modelo.com_ressalva else "mês fechado"
    elif modelo.pronto_para_fechar:
        estado = "mês aberto, pronto para fechar"
    else:
        estado = "mês aberto"
    partes = [
        _plural(divergencias, "divergência", "divergências"),
        f"{c.pendentes} sem justificativa",
    ]
    if c.aguardando_aprovacao:
        partes.append(f"{c.aguardando_aprovacao} aguardando aprovação")
    if c.total == 0:
        texto = f"{_mes_por_extenso(competencia)}: sem conciliação, {estado}."
    else:
        texto = f"{_mes_por_extenso(competencia)}: {', '.join(partes)}, {estado}."
    return _sucesso("resumo_do_mes", conexao, modelo, texto)


@servidor.tool(
    title="Conciliações do mês",
    description=(
        "Os pares conciliados vigentes de uma competência: para cada extrato do banco do mês, a "
        "conciliação da rodada mais recente, com data de execução, período do extrato e "
        "contagens por categoria. Use para saber quais conciliações compõem o mês."
    ),
    annotations=SO_LEITURA,
)
def conciliacoes_do_mes(
    competencia: ParametroCompetencia, ctx: Context
) -> Annotated[CallToolResult, ConciliacoesV1]:
    conexao = _conexao(ctx)
    if _competencia_invalida(competencia):
        return _erro("conciliacoes_do_mes", conexao, ERRO_COMPETENCIA, "competencia_invalida")
    with SessionLocal() as db:
        modelo = api_publica.conciliacoes_do_mes(db, conexao.empresa_id, competencia)
    texto = (
        f"{_mes_por_extenso(competencia)}: "
        f"{_plural(len(modelo.itens), 'conciliação vigente', 'conciliações vigentes')}."
    )
    return _sucesso("conciliacoes_do_mes", conexao, modelo, texto)


@servidor.tool(
    title="Divergências do mês",
    description=(
        "As linhas divergentes de uma competência, com categoria, data, valor (texto decimal "
        "em reais), lado (banco, sistema ou ambos) e situação. Nunca traz a descrição do "
        "lançamento nem o texto da justificativa. Paginada: `total` diz quantas há com os "
        "filtros; use `deslocamento` para as próximas páginas. Filtros opcionais: categoria "
        f"({', '.join(CATEGORIAS_DE_DIVERGENCIA)}) e situação ({', '.join(SITUACOES)})."
    ),
    annotations=SO_LEITURA,
)
def divergencias_do_mes(
    competencia: ParametroCompetencia,
    ctx: Context,
    categoria: Annotated[
        str | None,
        Field(description=f"Tipo de divergência: {', '.join(CATEGORIAS_DE_DIVERGENCIA)}."),
    ] = None,
    situacao: Annotated[
        str | None, Field(description=f"Situação da linha: {', '.join(SITUACOES)}.")
    ] = None,
    limite: Annotated[
        int, Field(description=f"Itens por página, de 1 a {LIMITE_MAXIMO}. Padrão {LIMITE_PADRAO}.")
    ] = LIMITE_PADRAO,
    deslocamento: Annotated[int, Field(description="Quantos itens pular. Padrão 0.")] = 0,
) -> Annotated[CallToolResult, DivergenciasV1]:
    nome = "divergencias_do_mes"
    conexao = _conexao(ctx)
    if _competencia_invalida(competencia):
        return _erro(nome, conexao, ERRO_COMPETENCIA, "competencia_invalida")
    if categoria is not None and categoria not in CATEGORIAS_DE_DIVERGENCIA:
        mensagem = (
            f"Categoria desconhecida. Use uma destas: {', '.join(CATEGORIAS_DE_DIVERGENCIA)}."
        )
        return _erro(nome, conexao, mensagem, "categoria_invalida")
    if situacao is not None and situacao not in SITUACOES:
        mensagem = f"Situação desconhecida. Use uma destas: {', '.join(SITUACOES)}."
        return _erro(nome, conexao, mensagem, "situacao_invalida")
    if not 1 <= limite <= LIMITE_MAXIMO:
        mensagem = f"O limite vai de 1 a {LIMITE_MAXIMO}."
        return _erro(nome, conexao, mensagem, "limite_invalido")
    if deslocamento < 0:
        return _erro(
            nome, conexao, "O deslocamento não pode ser negativo.", "deslocamento_invalido"
        )
    with SessionLocal() as db:
        modelo = api_publica.divergencias_do_mes(
            db, conexao.empresa_id, competencia, categoria, situacao, limite, deslocamento
        )
    texto = (
        f"{_mes_por_extenso(competencia)}: {_plural(modelo.total, 'divergência', 'divergências')}"
    )
    if categoria or situacao:
        texto += " com os filtros"
    if modelo.itens:
        texto += f"; mostrando {len(modelo.itens)} a partir da {deslocamento + 1}ª."
    else:
        texto += "."
    if modelo.total > deslocamento + len(modelo.itens) and modelo.itens:
        texto += " Há mais páginas."
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Fechamentos",
    description=(
        "Os fechamentos de mês da empresa, ativos e reabertos, dos mais recentes para os mais "
        "antigos, com as contagens congeladas no fechamento e `com_ressalva` (sem o texto da "
        "ressalva). Com `competencia` (AAAA-MM), só os daquele mês. Use para saber quando um "
        "mês foi fechado ou reaberto."
    ),
    annotations=SO_LEITURA,
)
def fechamentos(
    ctx: Context,
    competencia: Annotated[
        str | None,
        Field(description="Mês de referência AAAA-MM (ex.: 2026-09); sem ele, todos os meses."),
    ] = None,
) -> Annotated[CallToolResult, FechamentosV1]:
    conexao = _conexao(ctx)
    if competencia is not None and _competencia_invalida(competencia):
        return _erro("fechamentos", conexao, ERRO_COMPETENCIA, "competencia_invalida")
    with SessionLocal() as db:
        modelo = api_publica.fechamentos_da_empresa(db, conexao.empresa_id, competencia)
    alvo = f" de {_mes_por_extenso(competencia).lower()}" if competencia else ""
    if not modelo.itens:
        texto = f"Nenhum fechamento{alvo}."
    else:
        recente = modelo.itens[0]
        estado = "reaberto" if recente.reaberto_em else "ativo"
        texto = (
            f"{_plural(len(modelo.itens), 'fechamento', 'fechamentos')}{alvo}; o mais recente é "
            f"de {_mes_por_extenso(recente.competencia).lower()}, {estado}."
        )
    return _sucesso("fechamentos", conexao, modelo, texto)


# --- porta de entrada ----------------------------------------------------------------------

_limite = parse(LIMITE_MCP_POR_CONEXAO)
_gerenciador = None


def _resposta_json(status: int, corpo: dict, cabecalhos: dict | None = None) -> Callable:
    async def enviar(send: Send) -> None:
        dados = json.dumps(corpo).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(dados)).encode()),
        ]
        headers += [(k.lower().encode(), v.encode()) for k, v in (cabecalhos or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": dados})

    return enviar


def _origens_permitidas() -> set[str]:
    partes = urlsplit(settings.mcp_url_publica.strip())
    return {f"{partes.scheme}://{partes.netloc}", *ORIGENS_DOS_CLIENTES}


def _token_do_cabecalho(cabecalhos: dict[str, str]) -> str | None:
    esquema, _, token = cabecalhos.get("authorization", "").partition(" ")
    return token.strip() if esquema.lower() == "bearer" and token.strip() else None


def _autenticar(token: str | None) -> tuple[ConexaoAutenticada | None, bool, HTTPException | None]:
    with SessionLocal() as db:
        try:
            conexao, registrar = autenticar_token_mcp(db, token)
        except HTTPException as erro:
            return None, False, erro
    return conexao, registrar, None


def _segundos_ate_liberar(token: str) -> int | None:
    """Conta a requisição no limite da conexão (sha256 do token, sem banco) e
    devolve os segundos até liberar, se estourou."""
    if not limiter.enabled:
        return None
    chave = hashlib.sha256(token.encode()).hexdigest()
    if limiter.limiter.hit(_limite, "mcp", chave):
        return None
    reinicio, _ = limiter.limiter.get_window_stats(_limite, "mcp", chave)
    return max(1, math.ceil(reinicio - time.time()))


class PortaDoMcp:
    """A rota do /mcp: tudo o que vem antes do SDK (ver o docstring do módulo)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        cabecalhos = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not settings.mcp_habilitado:
            await _resposta_json(404, {"detail": "Not Found"})(send)
            return
        if not (settings.mcp_url_publica.strip() and settings.oauth_issuer.strip()):
            await _resposta_json(
                503, {"detail": "Conexão com assistentes de IA indisponível no momento."}
            )(send)
            return
        origem = cabecalhos.get("origin")
        if origem and origem.rstrip("/") not in _origens_permitidas():
            logger.info("mcp resultado=origem_recusada")
            corpo = {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Origin not allowed."}}
            await _resposta_json(403, corpo)(send)
            return

        token = _token_do_cabecalho(cabecalhos)
        conexao, registrar_uso, erro = await anyio.to_thread.run_sync(_autenticar, token)
        if erro is not None:
            if erro.status_code == 401:
                logger.info("mcp resultado=nao_autenticado")
            await _resposta_json(erro.status_code, {"detail": erro.detail}, erro.headers)(send)
            return
        segundos = _segundos_ate_liberar(token)
        if segundos is not None:
            logger.info("mcp resultado=limite conexao_id=%s", conexao.conexao_id)
            corpo = {"detail": "Limite de requisições excedido. Tente novamente em instantes."}
            await _resposta_json(429, corpo, {"Retry-After": str(segundos)})(send)
            return

        if _gerenciador is None:  # fora do lifespan do app
            await _resposta_json(
                503, {"detail": "Conexão com assistentes de IA indisponível no momento."}
            )(send)
            return
        scope.setdefault("state", {})["conexao_mcp"] = conexao
        await StreamableHTTPASGIApp(_gerenciador)(scope, receive, send)
        if registrar_uso:
            # Depois da resposta, numa sessão própria (como na chave de API).
            await anyio.to_thread.run_sync(registrar_uso_da_conexao, conexao.conexao_id)


@asynccontextmanager
async def ciclo_de_vida_do_mcp():
    """Sobe o gerenciador do Streamable HTTP junto com o app. Um gerenciador
    novo a cada ciclo: o do SDK só roda uma vez por instância."""
    global _gerenciador
    servidor.streamable_http_app(
        streamable_http_path=caminho_do_mcp(),
        stateless_http=True,
        json_response=True,
        # Host fica com o proxy da Railway; o Origin é conferido em PortaDoMcp.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    gerenciador = servidor.session_manager
    async with gerenciador.run():
        _gerenciador = gerenciador
        try:
            yield
        finally:
            _gerenciador = None


__all__ = ["PREFIXO_TOKEN_MCP", "PortaDoMcp", "caminho_do_mcp", "ciclo_de_vida_do_mcp", "servidor"]
