"""Migração da issue #118: saldo_final e saldo_data em extratos, nulos e com
CHECK de que vêm juntos. Sem backfill (ADR-002): extrato que já existia fica
com os dois nulos.

Mesmo esquema de banco descartável de tests/test_migracao_periodo_extrato_execucoes.py.
"""

import uuid
from decimal import Decimal

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "3e8b5d2a7c41"
REVISAO = "b41c7e2f9a58"


def _colunas_extratos(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("extratos")}
    finally:
        engine.dispose()


def _inserir_extrato(conn, empresa_id, saldo_final=None, saldo_data=None) -> uuid.UUID:
    colunas = ", saldo_final, saldo_data" if saldo_final or saldo_data else ""
    valores = ", :s, :d" if saldo_final or saldo_data else ""
    return conn.execute(
        text(
            "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, tamanho_bytes, "
            f"status, origem{colunas}) VALUES (gen_random_uuid(), :e, 'a.ofx', 'ofx', 0, "
            f"'concluido', 'banco'{valores}) RETURNING id"
        ),
        {"e": empresa_id, "s": saldo_final, "d": saldo_data},
    ).scalar_one()


def _inserir_empresa(conn) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO empresas (id, razao_social, cnpj) "
            "VALUES (gen_random_uuid(), 'E', '12345678000199') RETURNING id"
        )
    ).scalar_one()


def test_upgrade_cria_colunas_sem_backfill_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert "saldo_final" not in _colunas_extratos(url)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            antigo = _inserir_extrato(conn, _inserir_empresa(conn))
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", REVISAO)
    colunas = _colunas_extratos(url)
    assert colunas["saldo_final"]["nullable"] is True
    assert (colunas["saldo_final"]["type"].precision, colunas["saldo_final"]["type"].scale) == (
        14,
        2,
    )
    assert colunas["saldo_data"]["nullable"] is True
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            saldo_antigo = conn.execute(
                text("SELECT saldo_final, saldo_data FROM extratos WHERE id = :x"), {"x": antigo}
            ).one()
    finally:
        engine.dispose()
    assert saldo_antigo == (None, None)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    colunas = _colunas_extratos(url)
    assert "saldo_final" not in colunas and "saldo_data" not in colunas

    alembic_cli(url, "upgrade", "head")
    assert "saldo_final" in _colunas_extratos(url)


def test_check_exige_saldo_e_data_juntos(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_id = _inserir_empresa(conn)
            completo = _inserir_extrato(conn, empresa_id, Decimal("1249.25"), "2026-09-10")
            assert completo is not None

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _inserir_extrato(conn, empresa_id, saldo_final=Decimal("10.00"))

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _inserir_extrato(conn, empresa_id, saldo_data="2026-09-10")
    finally:
        engine.dispose()
