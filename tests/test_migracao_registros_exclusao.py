"""Migração da issue #68 (ADR-017): tabela `registros_exclusao`, sem FK para
`empresas`, porque a empresa do registro já foi apagada.

Mesmo esquema de banco descartável de tests/test_migracao_tokens_email.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect

REVISAO_ANTERIOR = "f2ffe353c4fe"
REVISAO = "3e8b5d2a7c41"


def _inspecionar(url):
    engine = create_engine(url)
    try:
        inspetor = inspect(engine)
        if not inspetor.has_table("registros_exclusao"):
            return None
        return {
            "colunas": {c["name"]: c for c in inspetor.get_columns("registros_exclusao")},
            "fks": inspetor.get_foreign_keys("registros_exclusao"),
        }
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) is None

    alembic_cli(url, "upgrade", REVISAO)
    tabela = _inspecionar(url)
    assert set(tabela["colunas"]) == {
        "id",
        "empresa_id",
        "cnpj_sha256",
        "contagens",
        "executado_em",
        "executado_por",
    }
    assert not any(c["nullable"] for c in tabela["colunas"].values())
    assert tabela["colunas"]["executado_em"]["type"].timezone is True
    assert tabela["fks"] == []

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) is None

    alembic_cli(url, "upgrade", "head")
    assert _inspecionar(url) is not None
