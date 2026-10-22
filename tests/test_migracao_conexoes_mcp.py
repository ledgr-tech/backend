"""Migração da issue #136: as tabelas do servidor de autorização OAuth e das
conexões MCP.

Mesmo esquema de banco descartável de tests/test_migracao_email_novo_token.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect

REVISAO_ANTERIOR = "f9b3d5a7c124"
REVISAO = "a1c3e5f7b902"
TABELAS = {
    "clientes_oauth",
    "pedidos_autorizacao_oauth",
    "conexoes_mcp",
    "codigos_oauth",
    "tokens_oauth",
}


def _tabelas(url) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names()) & TABELAS
    finally:
        engine.dispose()


def _unicas(url, tabela) -> set[tuple[str, ...]]:
    engine = create_engine(url)
    try:
        return {tuple(u["column_names"]) for u in inspect(engine).get_unique_constraints(tabela)}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_as_tabelas(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _tabelas(url) == set()

    alembic_cli(url, "upgrade", REVISAO)
    assert _tabelas(url) == TABELAS
    assert _unicas(url, "tokens_oauth") == {("hash_acesso",), ("hash_renovacao",)}
    assert _unicas(url, "codigos_oauth") == {("hash",)}
    assert _unicas(url, "pedidos_autorizacao_oauth") == {("hash",)}

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _tabelas(url) == set()

    alembic_cli(url, "upgrade", "head")
    assert _tabelas(url) == TABELAS
