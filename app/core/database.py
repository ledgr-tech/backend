"""Engine e sessão do SQLAlchemy.

Camada usada pelas repositories (Sprint 1 em diante). Aqui só definimos
engine/sessionmaker/dependency — nenhuma regra de negócio.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency do FastAPI: uma sessão por request, sempre fechada no final."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
