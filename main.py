from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.auth import router as auth_router
from app.api.conciliacoes import router as conciliacoes_router
from app.api.empresa import router as empresa_router
from app.api.execucoes import router as execucoes_router
from app.api.explicacoes import router as explicacoes_router
from app.api.extratos import router as extratos_router
from app.api.fechamentos import router as fechamentos_router
from app.api.me import router as me_router
from app.api.senha import router as senha_router
from app.core.frontend_url import avisar_frontend_url_no_startup
from app.core.logging import configurar_logs
from app.core.rate_limit import limiter

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
    yield


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth_router)
app.include_router(senha_router)
app.include_router(me_router)
app.include_router(empresa_router)
app.include_router(extratos_router)
app.include_router(conciliacoes_router)
app.include_router(execucoes_router)
app.include_router(fechamentos_router)
app.include_router(explicacoes_router)


@app.get("/health")
def health():
    return {"status": "ok"}
