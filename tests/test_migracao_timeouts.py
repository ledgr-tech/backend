"""Limites de tempo do `migrations/env.py` no modo online (issue #102).

Em 08/10 um pre-deploy do Railway ficou parado 300 s depois de conectar, sem
dizer o que esperava. O `env.py` passou a conectar com `connect_timeout=10` e a
rodar a migração com `lock_timeout = '10s'` e `statement_timeout = '90s'`, para
falhar com a causa no log antes dos 120 s do pre-deploy.

Os testes rodam o alembic como subprocesso, como em
`test_guarda_migracao_remota.py`: é o único jeito de exercitar o `env.py` de
verdade.
"""

import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Porta 1 no loopback: local (passa pela guarda da issue #65) e sem nada escutando.
URL_SEM_SERVIDOR = "postgresql+psycopg://ledgr:ledgr@127.0.0.1:1/ledgr"
LIMITE_SEGUNDOS = 30


def _alembic(*args: str, database_url: str) -> tuple[subprocess.CompletedProcess[str], float]:
    env = {k: v for k, v in os.environ.items() if k != "LEDGR_MIGRAR_BANCO_REMOTO"}
    env["DATABASE_URL"] = database_url
    inicio = time.monotonic()
    processo = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return processo, time.monotonic() - inicio


def test_sem_servidor_na_porta_falha_rapido_com_erro_de_conexao():
    processo, duracao = _alembic("upgrade", "head", database_url=URL_SEM_SERVIDOR)
    saida = processo.stdout + processo.stderr

    assert processo.returncode != 0
    assert duracao < LIMITE_SEGUNDOS, f"levou {duracao:.1f} s"
    assert "OperationalError" in saida
    assert "connection" in saida.lower()


def test_migracao_desiste_de_lock_preso_em_vez_de_esperar(postgres_disponivel):
    """Outra conexão segura a tabela do alembic; o alembic tem de desistir.

    `alembic current` lê `alembic_version` pela mesma conexão em que a migração
    roda. Com a tabela travada em ACCESS EXCLUSIVE, sem `lock_timeout` ele
    esperaria até o `timeout` do subprocess. Com o limite de 10 s, falha com
    "lock timeout" no stderr. Prova também que o SET vale na conexão da
    migração, e não numa conexão à parte.
    """
    if not postgres_disponivel:
        pytest.skip("Postgres real não disponível.")

    with engine.connect() as trava:
        trava.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
        processo, duracao = _alembic("current", database_url=settings.database_url)
        trava.rollback()
    saida = processo.stdout + processo.stderr

    assert processo.returncode != 0, saida
    assert "lock timeout" in saida
    assert duracao < LIMITE_SEGUNDOS, f"levou {duracao:.1f} s"
