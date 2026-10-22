"""Documentação pública da API v1 (issue #135, ADR-021).

`GET /v1/openapi.json` é o OpenAPI só das rotas `/v1`, e `GET /llms.txt`, um
texto curto em markdown para uma IA ler (app/api/llms.txt). Os dois são
públicos em produção, ao contrário do `/docs` e do OpenAPI do app, que
continuam atrás de `API_DOCS_HABILITADO`. O router `/v1` entra no app com
`include_in_schema=False`, para o OpenAPI do app não listar as rotas dele, e o
OpenAPI público sai só das rotas desse router.

`registrar_acesso` é o log de cada requisição às `/v1`: o id e o prefixo da
chave (quando ela foi aceita), a rota (o modelo, sem os ids) e o status.
Nunca o token nem dado de lançamento (issue #34).
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.v1 import router as v1_router

logger = logging.getLogger(__name__)

router = APIRouter()

PREFIXO = "/v1/"
LLMS_TXT = (Path(__file__).parent / "llms.txt").read_text(encoding="utf-8")
DESCRICAO = (
    "API pública do Ledgr, só de leitura: situação das conciliações bancárias, divergências "
    "por mês e fechamentos de cada empresa cliente. Autenticação pela chave de API no "
    "cabeçalho `Authorization: Bearer ledgr_...`, criada no app por admin ou gestor. "
    "60 requisições por minuto por chave. Nunca devolve descrição de lançamento, texto de "
    "justificativa, texto de ressalva nem nome de pessoa. De servidor para servidor, sem CORS. "
    "Resumo para IA em /llms.txt."
)

_esquema: dict | None = None


def openapi_publica() -> dict:
    global _esquema
    if _esquema is None:
        _esquema = get_openapi(
            title="Ledgr: API pública",
            version="1",
            description=DESCRICAO,
            routes=v1_router.routes,
        )
    return _esquema


@router.get("/v1/openapi.json", include_in_schema=False)
def especificacao() -> JSONResponse:
    return JSONResponse(openapi_publica())


@router.get("/llms.txt", include_in_schema=False)
def llms_txt() -> PlainTextResponse:
    return PlainTextResponse(LLMS_TXT, media_type="text/plain; charset=utf-8")


async def registrar_acesso(request: Request, chamar_proximo):
    resposta = await chamar_proximo(request)
    if request.url.path.startswith(PREFIXO) and request.url.path != "/v1/openapi.json":
        chave = getattr(request.state, "chave_api", None)
        rota = request.scope.get("route")
        logger.info(
            "api_publica chave_id=%s prefixo=%s rota=%s status=%s",
            chave.chave_id if chave else None,
            chave.prefixo if chave else None,
            getattr(rota, "path", "desconhecida"),
            resposta.status_code,
        )
    return resposta
