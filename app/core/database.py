"""Engine e sessão do SQLAlchemy.

Camada usada pelas repositories (Sprint 1 em diante). Aqui só definimos
engine/sessionmaker/dependency — nenhuma regra de negócio.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# connect_timeout curto (libpq, via psycopg): sem isso, uma tentativa de
# conectar num host inalcançável (porta não padrão bloqueada na rede de quem
# conecta, proxy do Railway com Public Access desligado, etc.) fica pendurada
# no timeout de TCP do SO — minutos, não segundos — em vez de falhar rápido.
# Não muda nada pra quem já conecta normalmente (CI, Railway acessível).
engine = create_engine(
    settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 10}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency do FastAPI: uma sessão por request, sempre fechada no final."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
