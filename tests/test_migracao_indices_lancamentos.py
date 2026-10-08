"""Migração da issue #15: índices simples em lancamentos (extrato_id, data, valor).

Roda alembic num banco descartável criado no mesmo servidor (subprocess com
DATABASE_URL apontando pra ele), pra nunca fazer upgrade/downgrade no
Postgres compartilhado de desenvolvimento.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect

REVISAO_ANTERIOR = "fdeeba077c27"
INDICES_NOVOS = {"ix_lancamentos_extrato_id", "ix_lancamentos_data", "ix_lancamentos_valor"}


def _indices_lancamentos(url) -> set[str]:
    engine = create_engine(url)
    try:
        return {i["name"] for i in inspect(engine).get_indexes("lancamentos")}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_indices(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    antes = _indices_lancamentos(url)
    assert not INDICES_NOVOS & antes
    assert "ix_lancamentos_empresa_id" in antes

    alembic_cli(url, "upgrade", "head")
    depois = _indices_lancamentos(url)
    assert INDICES_NOVOS <= depois
    assert "ix_lancamentos_empresa_id" in depois

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _indices_lancamentos(url) == antes

    alembic_cli(url, "upgrade", "head")
    assert _indices_lancamentos(url) == depois
