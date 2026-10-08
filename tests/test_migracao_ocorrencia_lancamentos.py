"""Migração da issue #18: coluna lancamentos.ocorrencia (INTEGER NOT NULL DEFAULT 1).

Mesmo esquema de banco descartável de test_migracao_indices_lancamentos.py.
"""

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "cbe4497d857c"


def _coluna_ocorrencia(url):
    engine = create_engine(url)
    try:
        colunas = {c["name"]: c for c in inspect(engine).get_columns("lancamentos")}
        return colunas.get("ocorrencia")
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_coluna_ocorrencia(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _coluna_ocorrencia(url) is None

    alembic_cli(url, "upgrade", "head")
    coluna = _coluna_ocorrencia(url)
    assert coluna is not None
    assert coluna["nullable"] is False
    assert str(coluna["default"]) == "1"

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _coluna_ocorrencia(url) is None

    alembic_cli(url, "upgrade", "head")
    assert _coluna_ocorrencia(url) is not None


def test_linhas_existentes_ganham_ocorrencia_1_no_upgrade(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_id = conn.execute(
                text(
                    "INSERT INTO empresas (id, razao_social, cnpj) "
                    "VALUES (gen_random_uuid(), 'E', '12345678000199') RETURNING id"
                )
            ).scalar_one()
            extrato_id = conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status) VALUES (gen_random_uuid(), :e, 'a.csv', 'csv', 0, "
                    "'concluido') RETURNING id"
                ),
                {"e": empresa_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO lancamentos (id, empresa_id, extrato_id, data, valor, "
                    "descricao, tipo, hash_dedup) VALUES (gen_random_uuid(), :e, :x, "
                    "'2026-09-05', 10.00, 'd', 'credito', 'h')"
                ),
                {"e": empresa_id, "x": extrato_id},
            )
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT ocorrencia FROM lancamentos")).scalar_one() == 1
    finally:
        engine.dispose()
