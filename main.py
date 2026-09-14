from fastapi import FastAPI

from app.api.extratos import router as extratos_router

app = FastAPI()

app.include_router(extratos_router)


@app.get("/health")
def health():
    return {"status": "ok"}
