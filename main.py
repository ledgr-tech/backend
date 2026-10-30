from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from app.api.admin import router as admin_router
from app.api.assinatura import router as assinatura_router
from app.api.assistente import router as assistente_router
from app.api.auth import router as auth_router
from app.api.chaves_api import router as chaves_api_router
from app.api.conciliacoes import router as conciliacoes_router
from app.api.conexoes_mcp import router as conexoes_mcp_router
from app.api.convites import router as convites_router
from app.api.documentacao_publica import registrar_acesso
from app.api.documentacao_publica import router as documentacao_publica_router
from app.api.empresa import router as empresa_router
from app.api.empresas import router as empresas_router
from app.api.equipe import router as equipe_router
from app.api.execucoes import router as execucoes_router
from app.api.explicacoes import router as explicacoes_router
from app.api.extratos import router as extratos_router
from app.api.fechamentos import router as fechamentos_router
from app.api.importacoes_ia import router as importacoes_ia_router
from app.api.justificativas import router as justificativas_router
from app.api.links_relatorio import router as links_relatorio_router
from app.api.links_relatorio import router_publico as relatorios_publicos_router
from app.api.mcp import PortaDoMcp, caminho_do_mcp, ciclo_de_vida_do_mcp
from app.api.me import router as me_router
from app.api.oauth import router as oauth_router
from app.api.plano import router as plano_router
from app.api.senha import router as senha_router
from app.api.stripe_webhook import router as stripe_webhook_router
from app.api.troca_email import router as troca_email_router
from app.api.v1 import router as v1_router
from app.core import conta_inativa
from app.core.config import settings
from app.core.frontend_url import avisar_frontend_url_no_startup
from app.core.logging import configurar_logs
from app.core.rate_limit import limite_excedido, limiter

# Antes de criar a app, para que o aviso do startup (FRONTEND_URL) já saia
# com formato e nível certos (issue #102).
configurar_logs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FRONTEND_URL errada (URL de deploy da Vercel, http fora de localhost) só
    # apareceria no log quando alguém pedisse recuperação de senha, dias depois
    # do deploy. Conferir no startup põe o motivo no log de deploy do Railway,
    # junto do resto do boot (issue #79). Não bloqueia o boot: o resto da API
    # não depende de e-mail.
    avisar_frontend_url_no_startup()
    # Servidor MCP (issue #136): o gerenciador do Streamable HTTP vive junto
    # com o app. Com MCP_HABILITADO desligado, o /mcp responde 404 mesmo assim.
    async with ciclo_de_vida_do_mcp():
        yield


# Com API_DOCS_HABILITADO=false (produção), /docs, /redoc e /openapi.json
# respondem 404 (issue #102). A documentação da API pública (/v1/openapi.json
# e /llms.txt, issue #135) é sempre pública e fica em
# app/api/documentacao_publica.py; o OpenAPI do app não lista as rotas /v1
# (include_in_schema=False abaixo).
# Sem CORS de propósito: a API pública é de servidor para servidor.
if settings.api_docs_habilitado:
    app = FastAPI(lifespan=lifespan)
else:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
app.middleware("http")(registrar_acesso)
app.add_exception_handler(RateLimitExceeded, limite_excedido)
app.add_exception_handler(conta_inativa.ContaInativaError, conta_inativa.tratar)

app.include_router(auth_router)
app.include_router(senha_router)
app.include_router(me_router)
app.include_router(troca_email_router)
app.include_router(empresa_router)
app.include_router(empresas_router)
app.include_router(equipe_router)
app.include_router(plano_router)
app.include_router(assinatura_router)
app.include_router(stripe_webhook_router)
app.include_router(convites_router)
app.include_router(extratos_router)
app.include_router(importacoes_ia_router)
app.include_router(conciliacoes_router)
app.include_router(execucoes_router)
app.include_router(fechamentos_router)
app.include_router(links_relatorio_router)
app.include_router(relatorios_publicos_router)
app.include_router(justificativas_router)
app.include_router(assistente_router)
app.include_router(explicacoes_router)
app.include_router(chaves_api_router)
app.include_router(v1_router, include_in_schema=False)
app.include_router(documentacao_publica_router)
app.include_router(oauth_router)
app.include_router(conexoes_mcp_router)
app.include_router(admin_router)
# Rota exata, sem Mount: um Mount redirecionaria /mcp para /mcp/.
app.router.add_route(caminho_do_mcp(), PortaDoMcp(), include_in_schema=False)


@app.get("/health")
def health():
    return {"status": "ok"}
