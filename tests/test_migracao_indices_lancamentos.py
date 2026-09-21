"""Migração da issue #15: índices simples em lancamentos (extrato_id, data, valor).

Roda alembic num banco descartável criado no mesmo servidor (subprocess com
DATABASE_URL apontando pra ele), pra nunca fazer upgrade/downgrade no
Postgres compartilhado de desenvolvimento.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import settings

RAIZ = Path(__file__).resolve().parent.parent
REVISAO_ANTERIOR = "fdeeba077c27"
INDICES_NOVOS = {"ix_lancamentos_extrato_id", "ix_lancamentos_data", "ix_lancamentos_valor"}


@pytest.fixture
def url_banco_descartavel(postgres_disponivel):
    if not postgres_disponivel:
        pytest.skip("Postgres real não disponível")
    nome = f"ledgr_mig_{uuid.uuid4().hex[:8]}"
    base = make_url(settings.database_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{nome}"'))
    except (OperationalError, ProgrammingError):
        admin.dispose()
        pytest.skip("Sem permissão pra criar banco descartável")
    try:
        yield base.set(database=nome)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{nome}" WITH (FORCE)'))
        admin.dispose()


def _alembic(url, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=RAIZ,
        env=env,
        check=True,
        capture_output=True,
    )


def _indices_lancamentos(url) -> set[str]:
    engine = create_engine(url)
    try:
        return {i["name"] for i in inspect(engine).get_indexes("lancamentos")}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_indices(url_banco_descartavel):
    url = url_banco_descartavel

    _alembic(url, "upgrade", REVISAO_ANTERIOR)
    antes = _indices_lancamentos(url)
    assert not INDICES_NOVOS & antes
    assert "ix_lancamentos_empresa_id" in antes

    _alembic(url, "upgrade", "head")
    depois = _indices_lancamentos(url)
    assert INDICES_NOVOS <= depois
    assert "ix_lancamentos_empresa_id" in depois

    _alembic(url, "downgrade", REVISAO_ANTERIOR)
    assert _indices_lancamentos(url) == antes

    _alembic(url, "upgrade", "head")
    assert _indices_lancamentos(url) == depois
