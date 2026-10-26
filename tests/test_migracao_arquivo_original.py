"""Migração da issue #139 (ADR-023): `extratos.arquivo_chave`, nula. Sem
backfill: os extratos que já existiam não têm original guardado (ADR-002).

Mesmo esquema de banco descartável de tests/test_migracao_saldo_extrato.py.
"""

import uuid

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "a1c3e5f7b902"
REVISAO = "c7e2a9d4f136"


def _colunas_extratos(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("extratos")}
    finally:
        engine.dispose()


def _inserir_extrato(conn) -> uuid.UUID:
    conta_id = conn.execute(
        text(
            "INSERT INTO contas (id, razao_social, cnpj) "
            "VALUES (:id, 'Conta', '11222333000181') RETURNING id"
        ),
        {"id": uuid.uuid4()},
    ).scalar_one()
    empresa_id = conn.execute(
        text(
            "INSERT INTO empresas (id, conta_id, razao_social, cnpj) "
            "VALUES (:id, :conta, 'Conta', '11222333000181') RETURNING id"
        ),
        {"id": uuid.uuid4(), "conta": conta_id},
    ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, tamanho_bytes, "
            "status, origem) VALUES (gen_random_uuid(), :e, 'a.ofx', 'ofx', 0, "
            "'concluido', 'banco') RETURNING id"
        ),
        {"e": empresa_id},
    ).scalar_one()


def test_upgrade_cria_arquivo_chave_nula_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert "arquivo_chave" not in _colunas_extratos(url)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            antigo = _inserir_extrato(conn)
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", REVISAO)
    assert _colunas_extratos(url)["arquivo_chave"]["nullable"] is True
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            chave = conn.execute(
                text("SELECT arquivo_chave FROM extratos WHERE id = :x"), {"x": antigo}
            ).scalar_one()
    finally:
        engine.dispose()
    assert chave is None

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert "arquivo_chave" not in _colunas_extratos(url)

    alembic_cli(url, "upgrade", "head")
    assert "arquivo_chave" in _colunas_extratos(url)
