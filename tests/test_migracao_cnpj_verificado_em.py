"""Migração da issue #119: empresas.cnpj_verificado_em, timestamptz nula, sem
backfill (as empresas que já existem ficam nulas).

Mesmo esquema de banco descartável de tests/test_migracao_saldo_extrato.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "b41c7e2f9a58"
REVISAO = "c7d2e94a1f36"


def _colunas_empresas(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("empresas")}
    finally:
        engine.dispose()


def test_upgrade_cria_coluna_nula_sem_backfill_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert "cnpj_verificado_em" not in _colunas_empresas(url)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            antiga = conn.execute(
                text(
                    "INSERT INTO empresas (id, razao_social, cnpj) "
                    "VALUES (gen_random_uuid(), 'E', '12345678000195') RETURNING id"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", REVISAO)
    coluna = _colunas_empresas(url)["cnpj_verificado_em"]
    assert coluna["nullable"] is True
    assert coluna["type"].timezone is True
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            verificado_em = conn.execute(
                text("SELECT cnpj_verificado_em FROM empresas WHERE id = :e"), {"e": antiga}
            ).scalar_one()
    finally:
        engine.dispose()
    assert verificado_em is None

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert "cnpj_verificado_em" not in _colunas_empresas(url)

    alembic_cli(url, "upgrade", "head")
    assert "cnpj_verificado_em" in _colunas_empresas(url)
