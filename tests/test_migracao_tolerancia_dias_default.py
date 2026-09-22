"""Migração da issue #22 (ADR-008): configuracoes.tolerancia_dias_default passa
de default 2 pra 0, com backfill das linhas que ainda estão em 2.

Mesmo esquema de banco descartável de test_migracao_ocorrencia_lancamentos.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "2a6441dd07d0"


def _default_da_coluna(url):
    engine = create_engine(url)
    try:
        colunas = {c["name"]: c for c in inspect(engine).get_columns("configuracoes")}
        return str(colunas["tolerancia_dias_default"]["default"])
    finally:
        engine.dispose()


def test_upgrade_muda_default_pra_0_e_downgrade_volta_pra_2(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _default_da_coluna(url) == "2"

    alembic_cli(url, "upgrade", "head")
    assert _default_da_coluna(url) == "0"

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _default_da_coluna(url) == "2"

    alembic_cli(url, "upgrade", "head")
    assert _default_da_coluna(url) == "0"


def test_linhas_existentes_em_2_viram_0_e_valores_customizados_sao_preservados(
    url_banco_descartavel,
):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for cnpj, tolerancia in (("12345678000199", 2), ("12345678000200", 5)):
                empresa_id = conn.execute(
                    text(
                        "INSERT INTO empresas (id, razao_social, cnpj) "
                        "VALUES (gen_random_uuid(), 'E', :c) RETURNING id"
                    ),
                    {"c": cnpj},
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO configuracoes (id, empresa_id, tolerancia_dias_default) "
                        "VALUES (gen_random_uuid(), :e, :t)"
                    ),
                    {"e": empresa_id, "t": tolerancia},
                )
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            valores = sorted(
                conn.execute(text("SELECT tolerancia_dias_default FROM configuracoes")).scalars()
            )
        assert valores == [0, 5]
    finally:
        engine.dispose()
