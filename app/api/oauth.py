"""O Ledgr como servidor de autorização OAuth da conexão MCP (issue #136,
ADR-022). O protocolo é do Authlib (app/services/oauth/servidor.py); aqui
ficam as rotas.

- `GET /.well-known/oauth-protected-resource` (e `.../mcp`, RFC 9728) e
  `GET /.well-known/oauth-authorization-server` (RFC 8414): a descoberta.
- `POST /oauth/register` (RFC 7591, JSON): só cliente público, com
  redirect_uris da lista permitida (app/services/oauth/redirects.py); fora
  dela, 400 `invalid_redirect_uri`. Rate limit por IP.
- `GET /oauth/authorize`: valida cliente, redirect_uri, `response_type=code`,
  PKCE S256, `resource` (o servidor MCP) e escopo. Erro de cliente ou de
  redirect_uri vira uma página de erro (400), sem redirecionar; os outros
  voltam ao redirect_uri com `error`, `state` e `iss`. Válido, grava o pedido
  (10 minutos, uso único, só o hash do id) e manda o navegador para a tela de
  consentimento do front, `<FRONTEND_URL>/autorizar-conexao?pedido=<id>`. A
  aprovação e a recusa estão em app/api/conexoes_mcp.py.
- `POST /oauth/token` (form-urlencoded): `authorization_code` com PKCE e
  `refresh_token` com rotação. Erros no formato do RFC 6749. Nenhuma chamada
  externa no caminho.
- `POST /oauth/revoke` (RFC 7009): o token de acesso informado deixa de
  valer; o de renovação revoga a conexão.

Com `MCP_HABILITADO` desligado, tudo responde 404; ligado sem
`MCP_URL_PUBLICA` ou `OAUTH_ISSUER`, 503 (`exigir_mcp`). Nunca loga token,
código, id de pedido nem `state`: só `oauth_* resultado=...`.
"""

import json
import logging
from collections import defaultdict
from datetime import timedelta
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import (
    LIMITE_AUTORIZACAO_OAUTH,
    LIMITE_REGISTRO_OAUTH,
    limiter,
)
from app.models import PedidoAutorizacaoOAuth
from app.services.oauth.servidor import (
    OAuth2Error,
    RequisicaoJson,
    RequisicaoOAuth,
    RespostaOAuth,
    ServidorOAuth,
    configurado,
    hash_do_segredo,
    mesmo_resource,
    metadados_do_recurso,
    metadados_do_servidor,
    novo_id_aleatorio,
    redirecionar_com_erro,
    url_mcp,
)

logger = logging.getLogger(__name__)

VALIDADE_PEDIDO = timedelta(minutes=10)
INDISPONIVEL = "Conexão com assistentes de IA indisponível no momento."


def exigir_mcp() -> None:
    """404 com a flag desligada (como se a rota não existisse); 503 sem as
    URLs configuradas."""
    if not settings.mcp_habilitado:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not configurado():
        logger.warning("mcp resultado=nao_configurado")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=INDISPONIVEL
        )


router = APIRouter(tags=["oauth"], dependencies=[Depends(exigir_mcp)])


def _listas(itens) -> dict[str, list[str]]:
    dados: dict[str, list[str]] = defaultdict(list)
    for chave, valor in itens:
        dados[chave].append(valor)
    return dict(dados)


def _json(resposta: RespostaOAuth) -> JSONResponse:
    return JSONResponse(
        resposta.corpo, status_code=resposta.status, headers=dict(resposta.cabecalhos)
    )


def _pagina_de_erro(motivo: str) -> HTMLResponse:
    """Erro de cliente ou de redirect_uri no authorize: o redirect_uri não é
    confiável, então o navegador fica aqui (RFC 6749 4.1.2.1)."""
    return HTMLResponse(
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<title>Ledgr: conexão não autorizada</title></head><body>"
        "<h1>Não foi possível conectar ao Ledgr</h1>"
        "<p>O aplicativo que pediu a conexão mandou um pedido inválido. Volte a ele e "
        "tente conectar de novo.</p>"
        f"<p><small>{escape(motivo)}</small></p></body></html>",
        status_code=http_status.HTTP_400_BAD_REQUEST,
    )


# --- descoberta ----------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def recurso_protegido() -> JSONResponse:
    return JSONResponse(metadados_do_recurso())


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
def servidor_de_autorizacao() -> JSONResponse:
    return JSONResponse(metadados_do_servidor())


# --- registro ------------------------------------------------------------------------------


def _registrar(db: Session, dados: dict, headers: dict) -> JSONResponse:
    resposta = ServidorOAuth(db).create_endpoint_response(
        "client_registration", RequisicaoJson("/oauth/register", dados, headers)
    )
    if resposta.status == http_status.HTTP_201_CREATED:
        db.commit()
        logger.info("oauth_registro resultado=registrado")
    else:
        db.rollback()
        logger.info("oauth_registro resultado=%s", resposta.corpo.get("error"))
    return _json(resposta)


@router.post("/oauth/register", status_code=http_status.HTTP_201_CREATED)
@limiter.limit(LIMITE_REGISTRO_OAUTH)
async def registrar_cliente(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> JSONResponse:
    try:
        dados = json.loads(await request.body() or b"null")
    except ValueError:
        dados = None
    if not isinstance(dados, dict):
        logger.info("oauth_registro resultado=corpo_invalido")
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "The body must be a JSON object.",
            },
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )
    return await run_in_threadpool(_registrar, db, dados, dict(request.headers))


# --- autorização ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
@limiter.limit(LIMITE_AUTORIZACAO_OAUTH)
def autorizar(request: Request, db: Annotated[Session, Depends(get_db)]):
    frontend_url = frontend_url_para_link()
    if not frontend_url:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=INDISPONIVEL
        )

    requisicao = RequisicaoOAuth(
        "GET", "/oauth/authorize", _listas(request.query_params.multi_items())
    )
    state = requisicao.payload.state
    try:
        grant = ServidorOAuth(db).get_consent_grant(requisicao)
    except OAuth2Error as erro:
        db.rollback()
        logger.info("oauth_authorize resultado=%s", erro.error)
        if erro.redirect_uri:
            return RedirectResponse(
                redirecionar_com_erro(erro.redirect_uri, erro.error, state), status_code=302
            )
        return _pagina_de_erro(erro.get_error_description() or erro.error)

    dados = requisicao.payload.data
    if len(requisicao.payload.datalist.get("resource", [])) != 1 or not mesmo_resource(
        dados.get("resource")
    ):
        logger.info("oauth_authorize resultado=invalid_target")
        return RedirectResponse(
            redirecionar_com_erro(grant.redirect_uri, "invalid_target", state), status_code=302
        )

    pedido = novo_id_aleatorio()
    db.add(
        PedidoAutorizacaoOAuth(
            hash=hash_do_segredo(pedido),
            client_id=grant.client.get_client_id(),
            redirect_uri=grant.redirect_uri,
            state=state,
            code_challenge=dados["code_challenge"],
            scope=requisicao.scope,
            resource=url_mcp(),
            expira_em=func.now() + VALIDADE_PEDIDO,
        )
    )
    db.commit()
    logger.info("oauth_authorize resultado=pedido")
    return RedirectResponse(
        f"{frontend_url.rstrip('/')}/autorizar-conexao?pedido={pedido}", status_code=302
    )


# --- token e revogação ---------------------------------------------------------------------


def _token(db: Session, dados: dict[str, list[str]], headers: dict) -> JSONResponse:
    requisicao = RequisicaoOAuth("POST", "/oauth/token", dados, headers)
    resposta = ServidorOAuth(db).create_token_response(requisicao)
    tipo = requisicao.payload.grant_type
    if resposta.status == http_status.HTTP_200_OK:
        db.commit()
        logger.info("oauth_token grant=%s resultado=emitido", tipo)
    else:
        db.rollback()
        logger.info("oauth_token grant=%s resultado=%s", tipo, resposta.corpo.get("error"))
    return _json(resposta)


@router.post("/oauth/token")
async def emitir_token(request: Request, db: Annotated[Session, Depends(get_db)]) -> JSONResponse:
    formulario = await request.form()
    return await run_in_threadpool(
        _token, db, _listas(formulario.multi_items()), dict(request.headers)
    )


def _revogar(db: Session, dados: dict[str, list[str]], headers: dict) -> JSONResponse:
    resposta = ServidorOAuth(db).create_endpoint_response(
        "revocation", RequisicaoOAuth("POST", "/oauth/revoke", dados, headers)
    )
    if resposta.status == http_status.HTTP_200_OK:
        db.commit()
        logger.info("oauth_revogacao resultado=ok")
    else:
        db.rollback()
        logger.info("oauth_revogacao resultado=%s", resposta.corpo.get("error"))
    return _json(resposta)


@router.post("/oauth/revoke")
async def revogar_token(request: Request, db: Annotated[Session, Depends(get_db)]) -> JSONResponse:
    formulario = await request.form()
    return await run_in_threadpool(
        _revogar, db, _listas(formulario.multi_items()), dict(request.headers)
    )
