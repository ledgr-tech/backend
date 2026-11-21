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
(app/services/api_publica.py) e a ampliação da issue #225
(app/services/api_publica_ampliada.py): nunca devolvem descrição de
lançamento, texto de justificativa, ressalva, nome de pessoa ou nome de
arquivo. Cada uma devolve o resultado estruturado (com `outputSchema`) e um
texto curto em português; entrada inválida é erro da ferramenta (`isError`),
nunca exceção HTTP. Nas ferramentas de mês, `competencia` omitida é o mês mais
recente com conciliação.

Ferramentas: empresa_conectada, meses_com_conciliacao, resumo_do_mes,
visao_do_mes, conciliacoes_do_mes, divergencias_do_mes,
detalhe_da_divergencia, pendencias_do_mes, lancamentos_do_mes, comparar_meses,
grupos_do_mes e fechamentos (12). Prompts: resumo_de_fechamento e
priorizar_divergencias.

Logs: o nome da ferramenta, o id da conexão e o resultado. Nunca o token nem o
conteúdo das respostas.
"""

import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
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

from app.core import conta_inativa
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
from app.services import api_publica, api_publica_ampliada
from app.services.api_publica import (
    LIMITE_MAXIMO,
    LIMITE_PADRAO,
    PADRAO_COMPETENCIA,
    ConciliacoesV1,
    FechamentosV1,
    MesesV1,
    ResumoV1,
)
from app.services.ia import detalhes as ia_detalhes

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
    "Ledgr é um sistema brasileiro de conciliação bancária: compara o extrato do banco com o do "
    "sistema (ERP) da empresa, aponta as divergências, registra justificativas e fecha o mês. "
    "Estas ferramentas leem, só para consulta, UMA empresa: a que o usuário escolheu ao "
    "conectar. Nenhuma recebe id de empresa.\n\n"
    "Por onde começar: resumo_do_mes (contagens) e visao_do_mes (dinheiro e saldos) dão a "
    "visão do mês; comparar_meses mostra a evolução entre meses; divergencias_do_mes lista as "
    "divergências (com filtros e ordenação) e detalhe_da_divergencia explica uma pelo `id`; "
    "lancamentos_do_mes acha e confere lançamentos por valor, data, tipo e situação; "
    "grupos_do_mes mostra os pagamentos em lote conciliados por agrupamento; pendencias_do_mes "
    "diz o que falta para fechar; fechamentos diz quando cada mês foi fechado; "
    "meses_com_conciliacao lista os meses disponíveis; empresa_conectada diz de qual empresa "
    "se trata.\n\n"
    "Competência é o mês no formato AAAA-MM (ex.: 2026-09). Nas ferramentas de mês, omitir a "
    "competência vale o mês mais recente com conciliação; a resposta traz a competência usada, "
    "e vale dizer ao usuário de que mês se trata. Se o usuário citar só o nome do mês, "
    "confirme o ano com meses_com_conciliacao.\n\n"
    'Valores vêm como texto decimal em reais, com duas casas e ponto (ex.: "1250.00"; '
    "negativo é saída); datas em ISO 8601. Não refaça contas: use os totais que as "
    "ferramentas devolvem. Listas vêm paginadas, com `total` e `truncado`: se `truncado` for "
    "verdadeiro, diga ao usuário que há mais ou filtre e pagine.\n\n"
    "Privacidade: as ferramentas nunca devolvem descrição de lançamento, texto de "
    "justificativa, texto de ressalva, nome de arquivo nem nome de pessoa. Um lançamento se "
    "identifica por data, valor, tipo e `id`. Não invente esses dados: se o usuário pedir, "
    "explique que o Ledgr não os envia ao assistente."
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


def _erro(
    ferramenta: str, conexao: ConexaoAutenticada, mensagem: str, resultado: str
) -> CallToolResult:
    logger.info(
        "mcp_ferramenta nome=%s conexao_id=%s resultado=%s",
        ferramenta,
        conexao.conexao_id,
        resultado,
    )
    return CallToolResult(content=[TextContent(type="text", text=mensagem)], is_error=True)


def _sucesso(
    ferramenta: str, conexao: ConexaoAutenticada, modelo: BaseModel, texto: str
) -> CallToolResult:
    logger.info("mcp_ferramenta nome=%s conexao_id=%s resultado=ok", ferramenta, conexao.conexao_id)
    return CallToolResult(
        content=[TextContent(type="text", text=texto)],
        structured_content=modelo.model_dump(mode="json"),
    )


def _competencia_invalida(competencia: str) -> bool:
    return not re.fullmatch(PADRAO_COMPETENCIA, competencia)


ERRO_COMPETENCIA = "Competência inválida: use o formato AAAA-MM, com mês de 01 a 12 (ex.: 2026-09)."
ERRO_ATE = "Mês `ate` inválido: use o formato AAAA-MM, com mês de 01 a 12 (ex.: 2026-09)."
ERRO_SEM_CONCILIACAO = (
    "Esta empresa ainda não tem conciliação em nenhum mês. Peça ao usuário para conciliar "
    "um extrato no Ledgr."
)
ERRO_DIVERGENCIA = (
    "Divergência não encontrada. Liste as divergências com divergencias_do_mes e passe o "
    "`id` de um item; o `id` muda se a conciliação do par for refeita."
)


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
    str | None,
    Field(
        description=(
            "Mês de referência no formato AAAA-MM, por exemplo 2026-09. "
            "Omitida: o mês mais recente com conciliação."
        )
    ),
]
ParametroValorMinimo = Annotated[
    Decimal | None,
    Field(
        description=(
            "Só valores de pelo menos isto, em reais (ex.: 1000 ou 1000.50). Vale para o valor "
            "absoluto: entradas e saídas entram igual."
        )
    ),
]
ParametroValorMaximo = Annotated[
    Decimal | None,
    Field(
        description=(
            "Só valores de no máximo isto, em reais. Vale para o valor absoluto, como `valor_minimo`."
        )
    ),
]


def _competencia_em_uso(
    db, nome: str, conexao: ConexaoAutenticada, competencia: str | None
) -> tuple[str | None, CallToolResult | None]:
    """A competência pedida ou, se omitida, a do mês mais recente com conciliação."""
    if competencia is not None:
        if _competencia_invalida(competencia):
            return None, _erro(nome, conexao, ERRO_COMPETENCIA, "competencia_invalida")
        return competencia, None
    recente = api_publica_ampliada.competencia_mais_recente(db, conexao.empresa_id)
    if recente is None:
        return None, _erro(nome, conexao, ERRO_SEM_CONCILIACAO, "sem_conciliacao")
    return recente, None


def _erro_sem_conciliacao(nome: str, conexao: ConexaoAutenticada, competencia: str):
    mensagem = (
        f"Não há conciliação em {_mes_por_extenso(competencia).lower()}. "
        "Use meses_com_conciliacao para ver os meses que têm."
    )
    return _erro(nome, conexao, mensagem, "sem_conciliacao")


class EmpresaConectada(BaseModel):
    """A empresa que a conexão lê."""

    razao_social: str = Field(description="Razão social da empresa.")
    cnpj: str = Field(description="CNPJ da empresa, sem máscara (14 caracteres).")


@servidor.tool(
    title="Empresa conectada",
    description=(
        "Devolve a razão social e o CNPJ da empresa que esta conexão lê. A conexão vale para "
        "uma empresa só, escolhida pelo usuário ao conectar: todas as outras ferramentas "
        "respondem sobre ela, e nenhuma recebe id de empresa. Use para confirmar de qual empresa "
        "se trata ou para citar o nome dela num resumo. Exemplo: 'de qual empresa você está "
        "falando?'."
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
        "descobrir quais meses consultar e quais ainda não foram fechados. Exemplos: 'quais "
        "meses já conciliei?', 'quais meses ainda não foram fechados?'."
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
        "Situação de uma competência em contagens: se o mês está fechado (e se foi com ressalva, "
        "sem o texto dela), se está pronto para fechar e quantas linhas há em cada categoria da "
        "conciliação e em cada situação (justificadas, aguardando aprovação, pendentes, linhas "
        "não lidas). Divergências são as linhas fora de match_exato, match_tolerancia e "
        "match_agrupado (um lote que fechou ao centavo: um lançamento contra vários do outro "
        "lado). Para dinheiro e saldos, use visao_do_mes; para o que falta para fechar, "
        "pendencias_do_mes. Sem `competencia`, o mês mais recente com conciliação. Exemplos: "
        "'como foi a conciliação de outubro?', 'quantas divergências tive em setembro?'."
    ),
    annotations=SO_LEITURA,
)
def resumo_do_mes(
    ctx: Context, competencia: ParametroCompetencia = None
) -> Annotated[CallToolResult, ResumoV1]:
    nome = "resumo_do_mes"
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        modelo = api_publica.resumo_do_mes(db, conexao.empresa_id, competencia)
    c = modelo.contagens
    divergencias = c.total - c.match_exato - c.match_tolerancia - c.match_agrupado
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
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Visão do mês",
    description=(
        "O mês como um todo, em dinheiro: para o banco e para o sistema, quanto entrou (créditos) "
        "e saiu (débitos, negativos), o líquido e o saldo final do extrato; a diferença líquida "
        "entre os dois lados, se os saldos finais batem, o percentual conciliado em quantidade e "
        "em valor, e os totais de tarifa bancária, duplicados e agrupamentos. Use para 'quanto "
        "entrou e saiu', 'o saldo bate?' e 'quanto foi de tarifa'. Para contagens por categoria, "
        "use resumo_do_mes. O saldo final só vem quando o lado tem um extrato só e o arquivo "
        "traz saldo. Sem `competencia`, o mês mais recente com conciliação. Exemplos: 'quanto "
        "entrou e quanto saiu em outubro, no banco e no sistema?', 'o saldo final do extrato "
        "bate com o do sistema?'."
    ),
    annotations=SO_LEITURA,
)
def visao_do_mes(
    ctx: Context, competencia: ParametroCompetencia = None
) -> Annotated[CallToolResult, api_publica_ampliada.VisaoDoMesV1]:
    nome = "visao_do_mes"
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        try:
            modelo = api_publica_ampliada.visao_do_mes(db, conexao.empresa_id, competencia)
        except api_publica_ampliada.SemConciliacao:
            return _erro_sem_conciliacao(nome, conexao, competencia)

    def reais(valor: Decimal) -> str:
        return ia_detalhes.formatar_valor(abs(valor))

    def percentual(valor: Decimal | None) -> str:
        return "sem dados" if valor is None else ia_detalhes.formatar_percentual(valor)

    texto = (
        f"{_mes_por_extenso(competencia)}: {percentual(modelo.percentual_conciliado_quantidade)} "
        f"das linhas conciliadas ({percentual(modelo.percentual_conciliado_valor)} do valor do "
        f"banco). Banco: entrou {reais(modelo.banco.entradas)} e saiu {reais(modelo.banco.saidas)}. "
        f"Sistema: entrou {reais(modelo.sistema.entradas)} e saiu "
        f"{reais(modelo.sistema.saidas)}. Diferença líquida (banco menos sistema): "
        f"{ia_detalhes.formatar_valor(modelo.diferenca_liquida)}."
    )
    if modelo.saldos_batem is None:
        texto += " Saldo final: indisponível em um dos lados."
    elif modelo.saldos_batem:
        texto += " Os saldos finais do banco e do sistema batem."
    else:
        texto += (
            f" Os saldos finais não batem: diferença de "
            f"{ia_detalhes.formatar_valor(modelo.diferenca_de_saldo)}."
        )
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Conciliações do mês",
    description=(
        "Os pares conciliados vigentes de uma competência: para cada extrato do banco do mês, a "
        "conciliação da rodada mais recente, com data de execução, período do extrato e "
        "contagens por categoria. Use para saber quais conciliações compõem o mês, quantas "
        "rodadas houve e quando rodaram; não traz nomes de arquivo. Sem `competencia`, o mês "
        "mais recente com conciliação. Exemplo: 'quando a conciliação de setembro foi feita?'."
    ),
    annotations=SO_LEITURA,
)
def conciliacoes_do_mes(
    ctx: Context, competencia: ParametroCompetencia = None
) -> Annotated[CallToolResult, ConciliacoesV1]:
    nome = "conciliacoes_do_mes"
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        modelo = api_publica.conciliacoes_do_mes(db, conexao.empresa_id, competencia)
    texto = (
        f"{_mes_por_extenso(competencia)}: "
        f"{_plural(len(modelo.itens), 'conciliação vigente', 'conciliações vigentes')}."
    )
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Divergências do mês",
    description=(
        "As linhas divergentes de uma competência, com `id`, categoria, data, valor (texto "
        "decimal em reais), lado (banco, sistema ou ambos) e situação. Nunca traz a descrição "
        "do lançamento nem o texto da justificativa. Filtros opcionais: `categoria`, "
        "`situacao`, `valor_minimo` e `valor_maximo` (valor absoluto, em reais). Ordena por data "
        "(padrão) ou por valor absoluto (`ordenar_por`), crescente ou decrescente (`ordem`). "
        "Paginada: `total` diz quantas há com os filtros e `truncado` se há mais além da "
        "página; use `deslocamento` para as próximas. Para explicar uma divergência, passe o "
        "`id` dela a detalhe_da_divergencia; para o que falta para fechar, use "
        "pendencias_do_mes. Sem `competencia`, o mês mais recente com conciliação. Exemplos: "
        "'quais são as dez maiores divergências do mês?' (ordenar_por=valor, ordem=desc, "
        "limite=10), 'quais divergências de valor passam de R$ 1.000?' "
        "(categoria=divergente_valor, valor_minimo=1000)."
    ),
    annotations=SO_LEITURA,
)
def divergencias_do_mes(
    ctx: Context,
    competencia: ParametroCompetencia = None,
    categoria: Annotated[
        api_publica.Categoria | None, Field(description="Só divergências deste tipo.")
    ] = None,
    situacao: Annotated[
        api_publica.Situacao | None,
        Field(
            description=(
                "Só linhas nesta situação: sem_justificativa (ninguém tratou), "
                "aguardando_aprovacao (justificada, falta aprovar), justificada (aprovada) ou "
                "conferida."
            )
        ),
    ] = None,
    valor_minimo: ParametroValorMinimo = None,
    valor_maximo: ParametroValorMaximo = None,
    ordenar_por: Annotated[
        api_publica.Ordenar,
        Field(description="Ordena por `data` (padrão) ou por `valor` (valor absoluto)."),
    ] = "data",
    ordem: Annotated[
        api_publica.Ordem,
        Field(description="`asc` (padrão, do menor para o maior) ou `desc`."),
    ] = "asc",
    limite: Annotated[
        int, Field(description=f"Itens por página, de 1 a {LIMITE_MAXIMO}. Padrão {LIMITE_PADRAO}.")
    ] = LIMITE_PADRAO,
    deslocamento: Annotated[int, Field(description="Quantos itens pular. Padrão 0.")] = 0,
) -> Annotated[CallToolResult, api_publica_ampliada.DivergenciasComIdV1]:
    nome = "divergencias_do_mes"
    conexao = _conexao(ctx)
    if competencia is not None and _competencia_invalida(competencia):
        return _erro(nome, conexao, ERRO_COMPETENCIA, "competencia_invalida")
    erro_pagina = _erro_de_pagina(nome, conexao, limite, LIMITE_MAXIMO, deslocamento)
    if erro_pagina is not None:
        return erro_pagina
    erro_valor = _erro_de_faixa_de_valor(nome, conexao, valor_minimo, valor_maximo)
    if erro_valor is not None:
        return erro_valor
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        modelo = api_publica_ampliada.divergencias_com_id(
            db,
            conexao.empresa_id,
            competencia,
            categoria=categoria,
            situacao=situacao,
            valor_minimo=valor_minimo,
            valor_maximo=valor_maximo,
            ordenar_por=ordenar_por,
            ordem=ordem,
            limit=limite,
            offset=deslocamento,
        )
    texto = (
        f"{_mes_por_extenso(competencia)}: {_plural(modelo.total, 'divergência', 'divergências')}"
    )
    if any(f is not None for f in (categoria, situacao, valor_minimo, valor_maximo)):
        texto += " com os filtros"
    if modelo.itens:
        texto += f"; mostrando {len(modelo.itens)} a partir da {deslocamento + 1}ª."
    else:
        texto += "."
    if modelo.truncado:
        texto += " Há mais páginas."
    return _sucesso(nome, conexao, modelo, texto)


def _erro_de_faixa_de_valor(
    nome: str,
    conexao: ConexaoAutenticada,
    minimo: Decimal | None,
    maximo: Decimal | None,
) -> CallToolResult | None:
    if any(v is not None and (v < 0 or not v.is_finite()) for v in (minimo, maximo)):
        mensagem = (
            "Os valores mínimo e máximo não podem ser negativos: o filtro vale para o valor "
            "absoluto (R$ 1.000 pega entradas e saídas de R$ 1.000)."
        )
        return _erro(nome, conexao, mensagem, "valor_invalido")
    if minimo is not None and maximo is not None and minimo > maximo:
        return _erro(
            nome, conexao, "O valor mínimo não pode ser maior que o máximo.", "valor_invalido"
        )
    return None


@servidor.tool(
    title="Detalhe da divergência",
    description=(
        "Uma divergência explicada com fatos calculados, sem IA: o lançamento da linha e, quando "
        "há, o do outro lado (data, valor e tipo; nunca a descrição), a diferença de valor e de "
        "dias, a categoria, a tolerância de dias da rodada, a situação da decisão (sem o texto "
        "da justificativa) e uma explicação em português com os números. Precisa do `id` de um "
        "item de divergencias_do_mes (ou de pendencias_do_mes). O `id` muda se a conciliação do "
        "par for refeita: se der 'não encontrada', liste de novo. Exemplo: 'por que essa "
        "divergência de R$ 350,00 aconteceu?' (ache-a em divergencias_do_mes pelo valor e passe "
        "o `id`)."
    ),
    annotations=SO_LEITURA,
)
def detalhe_da_divergencia(
    ctx: Context,
    id: Annotated[
        str,
        Field(description="Identificador (UUID) da divergência, o `id` de divergencias_do_mes."),
    ],
) -> Annotated[CallToolResult, api_publica_ampliada.DetalheDaDivergenciaV1]:
    nome = "detalhe_da_divergencia"
    conexao = _conexao(ctx)
    try:
        divergencia_id = uuid.UUID(id)
    except ValueError:
        return _erro(nome, conexao, ERRO_DIVERGENCIA, "id_invalido")
    with SessionLocal() as db:
        try:
            modelo = api_publica_ampliada.detalhe_da_divergencia(
                db, conexao.empresa_id, divergencia_id
            )
        except api_publica_ampliada.DivergenciaNaoEncontrada:
            return _erro(nome, conexao, ERRO_DIVERGENCIA, "nao_encontrada")
    texto = (
        f"Divergência de {_mes_por_extenso(modelo.competencia).lower()} "
        f"({modelo.situacao.replace('_', ' ')}). {modelo.explicacao}"
    )
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Pendências do mês",
    description=(
        "O que falta para fechar um mês: as divergências sem justificativa e as com "
        "justificativa aguardando aprovação (contagem e ids, das de maior valor para as de "
        "menor), as linhas não lidas, `pode_fechar` (sem ressalva), `pode_fechar_com_ressalva` e "
        "a lista de `motivos` que impedem. Regra do Ledgr: justificativa não aprovada impede "
        "o fechamento sempre; divergência sem justificativa e linha não lida só impedem "
        "fechar sem ressalva. Use os ids com detalhe_da_divergencia. Não traz o texto das "
        "justificativas. Sem `competencia`, o mês mais recente com conciliação. Exemplos: "
        "'outubro pode ser fechado? o que falta?', 'o que está aguardando aprovação?'."
    ),
    annotations=SO_LEITURA,
)
def pendencias_do_mes(
    ctx: Context, competencia: ParametroCompetencia = None
) -> Annotated[CallToolResult, api_publica_ampliada.PendenciasV1]:
    nome = "pendencias_do_mes"
    conexao = _conexao(ctx)
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        try:
            modelo = api_publica_ampliada.pendencias_do_mes(db, conexao.empresa_id, competencia)
        except api_publica_ampliada.SemConciliacao:
            return _erro_sem_conciliacao(nome, conexao, competencia)
    mes = _mes_por_extenso(competencia)
    if modelo.pode_fechar:
        texto = f"{mes}: pode ser fechado, sem pendências."
    else:
        motivos = ", ".join(
            _quantidade_do_motivo(m) for m in modelo.motivos if m.codigo != "mes_fechado"
        )
        if modelo.fechado:
            texto = f"{mes}: já está fechado."
        elif modelo.pode_fechar_com_ressalva:
            texto = f"{mes}: não fecha sem ressalva ({motivos}); fecha com ressalva."
        else:
            texto = f"{mes}: não pode ser fechado ({motivos})."
    return _sucesso(nome, conexao, modelo, texto)


def _quantidade_do_motivo(motivo: api_publica_ampliada.MotivoV1) -> str:
    rotulos = {
        "justificativas_aguardando_aprovacao": (
            "justificativa aguardando aprovação",
            "justificativas aguardando aprovação",
        ),
        "divergencias_sem_justificativa": (
            "divergência sem justificativa",
            "divergências sem justificativa",
        ),
        "linhas_nao_lidas": ("linha não lida", "linhas não lidas"),
    }
    singular, plural = rotulos.get(motivo.codigo, (motivo.codigo, motivo.codigo))
    return _plural(motivo.quantidade, singular, plural)


@servidor.tool(
    title="Lançamentos do mês",
    description=(
        "Os lançamentos (linhas dos extratos) do mês, do banco e do sistema, cada um com `id`, "
        "data, valor, tipo (credito ou debito), lado e `situacao` (conciliado ou a categoria da "
        "divergência em que caiu), mais o `divergencia_id` quando é divergência. Nunca traz a "
        "descrição do lançamento: identifique um lançamento por valor, data e tipo. Filtros "
        "opcionais: `lado`, `tipo`, `situacao`, `valor_minimo` e `valor_maximo` (valor absoluto, "
        "em reais), `data` (um dia) ou `data_inicial` e `data_final`. Ordena por data (padrão) "
        "ou por valor absoluto. Paginada: `total` diz quantos há com os filtros e `truncado` se "
        "há mais além da página. Use para achar ou conferir lançamentos; para a lista de "
        "divergências use divergencias_do_mes, e passe o `divergencia_id` a "
        "detalhe_da_divergencia para explicar uma. Sem `competencia`, o mês mais recente com "
        "conciliação. Exemplos: 'existe um lançamento de R$ 1.250,00 em outubro? foi "
        "conciliado?' (valor_minimo=1250, valor_maximo=1250), 'quais lançamentos do banco no dia "
        "12 ficaram sem correspondência?' (lado=banco, data=2026-10-12, "
        "situacao=sem_correspondencia), 'liste os débitos acima de R$ 5.000' (tipo=debito, "
        "valor_minimo=5000)."
    ),
    annotations=SO_LEITURA,
)
def lancamentos_do_mes(
    ctx: Context,
    competencia: ParametroCompetencia = None,
    lado: Annotated[
        api_publica_ampliada.LadoDoLancamento | None,
        Field(description="Só lançamentos deste extrato: banco ou sistema (ERP)."),
    ] = None,
    tipo: Annotated[
        api_publica_ampliada.TipoDoLancamento | None,
        Field(description="credito (entrada) ou debito (saída)."),
    ] = None,
    situacao: Annotated[
        api_publica_ampliada.SituacaoDoLancamento | None,
        Field(
            description=(
                "Como a conciliação tratou o lançamento: conciliado (par exato, por tolerância "
                "de data ou agrupado) ou a categoria da divergência (divergente_valor, "
                "divergente_data, sem_correspondencia, duplicado, tarifa_bancaria)."
            )
        ),
    ] = None,
    valor_minimo: ParametroValorMinimo = None,
    valor_maximo: ParametroValorMaximo = None,
    data: Annotated[
        date | None,
        Field(description="Só lançamentos deste dia, ISO 8601 (ex.: 2026-10-12)."),
    ] = None,
    data_inicial: Annotated[
        date | None, Field(description="Só lançamentos desta data em diante, inclusive.")
    ] = None,
    data_final: Annotated[
        date | None, Field(description="Só lançamentos até esta data, inclusive.")
    ] = None,
    ordenar_por: Annotated[
        api_publica.Ordenar,
        Field(description="Ordena por `data` (padrão) ou por `valor` (valor absoluto)."),
    ] = "data",
    ordem: Annotated[
        api_publica.Ordem,
        Field(description="`asc` (padrão, do menor para o maior) ou `desc`."),
    ] = "asc",
    limite: Annotated[
        int, Field(description=f"Itens por página, de 1 a {LIMITE_MAXIMO}. Padrão {LIMITE_PADRAO}.")
    ] = LIMITE_PADRAO,
    deslocamento: Annotated[int, Field(description="Quantos itens pular. Padrão 0.")] = 0,
) -> Annotated[CallToolResult, api_publica_ampliada.LancamentosV1]:
    nome = "lancamentos_do_mes"
    conexao = _conexao(ctx)
    if competencia is not None and _competencia_invalida(competencia):
        return _erro(nome, conexao, ERRO_COMPETENCIA, "competencia_invalida")
    erro_pagina = _erro_de_pagina(nome, conexao, limite, LIMITE_MAXIMO, deslocamento)
    if erro_pagina is not None:
        return erro_pagina
    erro_valor = _erro_de_faixa_de_valor(nome, conexao, valor_minimo, valor_maximo)
    if erro_valor is not None:
        return erro_valor
    if data is not None and (data_inicial is not None or data_final is not None):
        mensagem = (
            "Use `data` (um dia) ou `data_inicial` e `data_final` (um intervalo), não os dois."
        )
        return _erro(nome, conexao, mensagem, "data_invalida")
    if data_inicial is not None and data_final is not None and data_inicial > data_final:
        return _erro(nome, conexao, "A data inicial não pode ser depois da final.", "data_invalida")
    if data is not None:
        data_inicial = data_final = data
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        modelo = api_publica_ampliada.lancamentos_do_mes(
            db,
            conexao.empresa_id,
            competencia,
            lado=lado,
            tipo=tipo,
            situacao=situacao,
            valor_minimo=valor_minimo,
            valor_maximo=valor_maximo,
            data_inicial=data_inicial,
            data_final=data_final,
            ordenar_por=ordenar_por,
            ordem=ordem,
            limit=limite,
            offset=deslocamento,
        )
    texto = f"{_mes_por_extenso(competencia)}: {_plural(modelo.total, 'lançamento', 'lançamentos')}"
    if any(f is not None for f in (lado, tipo, situacao, valor_minimo, valor_maximo)) or (
        data_inicial is not None or data_final is not None
    ):
        texto += " com os filtros"
    if modelo.itens:
        texto += f"; mostrando {len(modelo.itens)} a partir do {deslocamento + 1}º."
    else:
        texto += "."
    if modelo.truncado:
        texto += " Há mais páginas."
    return _sucesso(nome, conexao, modelo, texto)


@servidor.tool(
    title="Comparar meses",
    description=(
        "A série dos últimos meses com conciliação, do mais antigo para o mais recente, cada um "
        "com as linhas, as conciliadas, as divergências por categoria (divergente_valor, "
        "divergente_data, sem_correspondencia, duplicado, tarifa_bancaria) e a `variacao` "
        "(diferença) para o mês anterior da série, em quantidade e em percentual do total de "
        "divergências. Use para comparar meses e ver tendência. Para dinheiro e saldos de um "
        "mês, use visao_do_mes. `quantidade` vai de 1 a "
        f"{api_publica_ampliada.QUANTIDADE_MAXIMA_DE_MESES} (padrão "
        f"{api_publica_ampliada.QUANTIDADE_PADRAO_DE_MESES}); `ate` é o último mês da série "
        "(padrão: o mais recente com conciliação). Exemplos: 'como outubro se compara com "
        "setembro?' (quantidade=2, ate=2026-10), 'como evoluíram as divergências nos últimos "
        "seis meses?'."
    ),
    annotations=SO_LEITURA,
)
def comparar_meses(
    ctx: Context,
    quantidade: Annotated[
        int,
        Field(
            description=(
                f"Quantos meses, de 1 a {api_publica_ampliada.QUANTIDADE_MAXIMA_DE_MESES}. "
                f"Padrão {api_publica_ampliada.QUANTIDADE_PADRAO_DE_MESES}."
            )
        ),
    ] = api_publica_ampliada.QUANTIDADE_PADRAO_DE_MESES,
    ate: Annotated[
        str | None,
        Field(
            description=(
                "Último mês da série, AAAA-MM. Omitido: o mês mais recente com conciliação."
            )
        ),
    ] = None,
) -> Annotated[CallToolResult, api_publica_ampliada.ComparacaoV1]:
    nome = "comparar_meses"
    conexao = _conexao(ctx)
    if ate is not None and _competencia_invalida(ate):
        return _erro(nome, conexao, ERRO_ATE, "ate_invalido")
    maximo = api_publica_ampliada.QUANTIDADE_MAXIMA_DE_MESES
    if not 1 <= quantidade <= maximo:
        return _erro(
            nome, conexao, f"A quantidade de meses vai de 1 a {maximo}.", "quantidade_invalida"
        )
    with SessionLocal() as db:
        modelo = api_publica_ampliada.comparar_meses(db, conexao.empresa_id, quantidade, ate)
    if not modelo.itens:
        return _erro(nome, conexao, ERRO_SEM_CONCILIACAO, "sem_conciliacao")
    ultimo = modelo.itens[-1]
    texto = (
        f"{_plural(modelo.quantidade, 'mês', 'meses')} com conciliação, de "
        f"{_mes_por_extenso(modelo.itens[0].competencia).lower()} a "
        f"{_mes_por_extenso(ultimo.competencia).lower()}: {_plural(ultimo.divergencias.total, 'divergência', 'divergências')} "
        f"no último"
    )
    if ultimo.variacao is not None:
        delta = ultimo.variacao.total
        texto += (
            ", igual ao mês anterior"
            if delta == 0
            else f", {abs(delta)} {'a mais' if delta > 0 else 'a menos'} que no mês anterior"
        )
    return _sucesso(nome, conexao, modelo, texto + ".")


@servidor.tool(
    title="Grupos do mês",
    description=(
        "Os agrupamentos do mês: um lançamento de um lado cuja soma bate, ao centavo, com a de "
        "vários do outro (pagamento em lote, um lançamento de R$ 4.000 contra dois de R$ 2.000). "
        "Cada grupo traz o lado do lançamento único, a `soma` que fechou, o lançamento único "
        "(`id`, data e valor) e os do outro lado (até "
        f"{api_publica_ampliada.MEMBROS_POR_GRUPO}, com a quantidade total). Eles contam como "
        "conciliados, não como divergência. Dos de maior soma para os de menor, paginados em "
        f"até {api_publica_ampliada.GRUPOS_LIMITE_MAXIMO} grupos. Para só o total de grupos, "
        "veja `agrupamentos` em visao_do_mes. Sem `competencia`, o mês mais recente com "
        "conciliação. Exemplo: 'quais lançamentos foram conciliados por agrupamento e como a "
        "soma foi formada?'."
    ),
    annotations=SO_LEITURA,
)
def grupos_do_mes(
    ctx: Context,
    competencia: ParametroCompetencia = None,
    limite: Annotated[
        int,
        Field(
            description=(
                f"Grupos por página, de 1 a {api_publica_ampliada.GRUPOS_LIMITE_MAXIMO}. "
                f"Padrão {api_publica_ampliada.GRUPOS_LIMITE_PADRAO}."
            )
        ),
    ] = api_publica_ampliada.GRUPOS_LIMITE_PADRAO,
    deslocamento: Annotated[int, Field(description="Quantos grupos pular. Padrão 0.")] = 0,
) -> Annotated[CallToolResult, api_publica_ampliada.GruposV1]:
    nome = "grupos_do_mes"
    conexao = _conexao(ctx)
    if competencia is not None and _competencia_invalida(competencia):
        return _erro(nome, conexao, ERRO_COMPETENCIA, "competencia_invalida")
    erro_pagina = _erro_de_pagina(
        nome, conexao, limite, api_publica_ampliada.GRUPOS_LIMITE_MAXIMO, deslocamento
    )
    if erro_pagina is not None:
        return erro_pagina
    with SessionLocal() as db:
        competencia, erro = _competencia_em_uso(db, nome, conexao, competencia)
        if erro is not None:
            return erro
        modelo = api_publica_ampliada.grupos_do_mes(
            db, conexao.empresa_id, competencia, limite, deslocamento
        )
    mes = _mes_por_extenso(competencia)
    texto = f"{mes}: {_plural(modelo.total, 'agrupamento', 'agrupamentos')}"
    if modelo.itens:
        texto += f"; mostrando {len(modelo.itens)} a partir do {deslocamento + 1}º"
    texto += ". Há mais páginas." if modelo.truncado else "."
    return _sucesso(nome, conexao, modelo, texto)


def _erro_de_pagina(
    nome: str, conexao: ConexaoAutenticada, limite: int, maximo: int, deslocamento: int
) -> CallToolResult | None:
    if not 1 <= limite <= maximo:
        return _erro(nome, conexao, f"O limite vai de 1 a {maximo}.", "limite_invalido")
    if deslocamento < 0:
        return _erro(
            nome, conexao, "O deslocamento não pode ser negativo.", "deslocamento_invalido"
        )
    return None


@servidor.tool(
    title="Fechamentos",
    description=(
        "Os fechamentos de mês da empresa, ativos e reabertos, dos mais recentes para os mais "
        "antigos, com as contagens congeladas no fechamento e `com_ressalva` (sem o texto da "
        "ressalva). Com `competencia` (AAAA-MM), só os daquele mês. Use para saber quando um "
        "mês foi fechado ou reaberto; para saber se um mês aberto pode fechar, use "
        "pendencias_do_mes. Exemplos: 'quando foi fechado setembro?', 'quais meses foram "
        "fechados com ressalva?'."
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


# --- prompts (M4) ----------------------------------------------------------------------------


def _mes_do_prompt(competencia: str | None) -> str:
    return f"o mês {competencia}" if competencia else "o mês mais recente com conciliação"


@servidor.prompt(
    name="resumo_de_fechamento",
    title="Resumo de fechamento",
    description=(
        "Monta um resumo do fechamento de um mês para o usuário mandar ao cliente da empresa."
    ),
)
def resumo_de_fechamento(competencia: str | None = None) -> str:
    return (
        f"Faça um resumo do fechamento de {_mes_do_prompt(competencia)} para eu mandar ao "
        "cliente. Use as ferramentas do Ledgr, nesta ordem: empresa_conectada (nome da "
        "empresa), resumo_do_mes e visao_do_mes (conciliação, entradas, saídas e saldos), "
        "pendencias_do_mes (se pode fechar e o que falta), divergencias_do_mes com "
        "ordenar_por=valor, ordem=desc e limite=5 (as maiores divergências) e fechamentos (se e "
        "quando o mês foi fechado). Escreva em português, em tom profissional e curto: situação "
        "geral, números principais, o que ficou pendente e o próximo passo. Use só os números "
        "que as ferramentas devolveram e não invente descrição de lançamento nem nomes."
    )


@servidor.prompt(
    name="priorizar_divergencias",
    title="Priorizar divergências",
    description="Lista por onde começar a tratar as divergências de um mês, das mais importantes.",
)
def priorizar_divergencias(competencia: str | None = None) -> str:
    return (
        f"Quero tratar as divergências de {_mes_do_prompt(competencia)}. Use pendencias_do_mes "
        "para ver o que falta justificar ou aprovar, divergencias_do_mes com "
        "situacao=sem_justificativa, ordenar_por=valor e ordem=desc para as maiores, e "
        "detalhe_da_divergencia nas três primeiras. Proponha uma ordem de ataque: primeiro o que "
        "tem maior valor ou impede o fechamento, depois o resto, e diga em uma frase o que "
        "provavelmente explica cada uma, com base só nos fatos calculados que as ferramentas "
        "devolveram. Não invente descrição de lançamento."
    )


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
        except conta_inativa.ContaInativaError:
            # Conta inativa (issue #156): o mesmo 403 das outras rotas.
            return None, False, HTTPException(status_code=403, detail=conta_inativa.corpo())
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
            corpo = erro.detail if isinstance(erro.detail, dict) else {"detail": erro.detail}
            await _resposta_json(erro.status_code, corpo, erro.headers)(send)
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
