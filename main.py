from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.auth import router as auth_router
from app.api.conciliacoes import router as conciliacoes_router
from app.api.execucoes import router as execucoes_router
from app.api.explicacoes import router as explicacoes_router
from app.api.extratos import router as extratos_router
from app.core.rate_limit import limiter

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth_router)
app.include_router(extratos_router)
app.include_router(conciliacoes_router)
app.include_router(execucoes_router)
app.include_router(explicacoes_router)


@app.get("/health")
def health():
    return {"status": "ok"}
