"""Migração da issue #137: `usos_assistente` (conta e instante) e
`perguntas_sem_resposta` (pergunta mascarada, tela e instante, sem conta).

Mesmo esquema de banco descartável de tests/test_migracao_convites_papel.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect

REVISAO_ANTERIOR = "b8d4f2a6c391"
REVISAO = "c3e9a1d5f724"


def _tabelas(url):
    engine = create_engine(url)
    try:
        inspecao = inspect(engine)
        return {
            tabela: {c["name"] for c in inspecao.get_columns(tabela)}
            for tabela in inspecao.get_table_names()
            if tabela in ("usos_assistente", "perguntas_sem_resposta")
        }
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_as_duas_tabelas(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _tabelas(url) == {}

    alembic_cli(url, "upgrade", REVISAO)
    assert _tabelas(url) == {
        "usos_assistente": {"id", "conta_id", "criado_em"},
        "perguntas_sem_resposta": {"id", "pergunta", "tela", "criado_em"},
    }

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _tabelas(url) == {}

    alembic_cli(url, "upgrade", "head")
    assert set(_tabelas(url)) == {"usos_assistente", "perguntas_sem_resposta"}
