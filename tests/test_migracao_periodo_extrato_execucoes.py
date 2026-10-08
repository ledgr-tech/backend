"""Migração da issue #82 (ADR-010): periodo_inicio/periodo_fim em extratos
(com backfill a partir dos lançamentos já gravados) e índice composto
(empresa_id, extrato_banco_id, criado_em) em execucoes_conciliacao.

Mesmo esquema de banco descartável de tests/test_migracao_ocorrencia_lancamentos.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "aa7ea0fc848f"


def _colunas_extratos(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("extratos")}
    finally:
        engine.dispose()


def _indices_execucoes(url):
    engine = create_engine(url)
    try:
        return {i["name"]: i for i in inspect(engine).get_indexes("execucoes_conciliacao")}
    finally:
        engine.dispose()


def test_upgrade_cria_colunas_e_indice_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert "periodo_inicio" not in _colunas_extratos(url)
    assert "periodo_fim" not in _colunas_extratos(url)
    assert (
        "ix_execucoes_conciliacao_empresa_id_extrato_banco_id_criado_em"
        not in _indices_execucoes(url)
    )

    alembic_cli(url, "upgrade", "head")
    colunas = _colunas_extratos(url)
    assert colunas["periodo_inicio"]["nullable"] is True
    assert colunas["periodo_fim"]["nullable"] is True
    indice = _indices_execucoes(url)[
        "ix_execucoes_conciliacao_empresa_id_extrato_banco_id_criado_em"
    ]
    assert indice["column_names"] == ["empresa_id", "extrato_banco_id", "criado_em"]

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert "periodo_inicio" not in _colunas_extratos(url)
    assert (
        "ix_execucoes_conciliacao_empresa_id_extrato_banco_id_criado_em"
        not in _indices_execucoes(url)
    )

    alembic_cli(url, "upgrade", "head")
    assert "periodo_inicio" in _colunas_extratos(url)


def _inserir_empresa(conn) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO empresas (id, razao_social, cnpj) "
            "VALUES (gen_random_uuid(), 'E', '12345678000199') RETURNING id"
        )
    ).scalar_one()


def _inserir_extrato(conn, empresa_id, nome_arquivo="a.csv") -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, tamanho_bytes, "
            "status, origem) VALUES (gen_random_uuid(), :e, :n, 'csv', 0, 'concluido', "
            "'banco') RETURNING id"
        ),
        {"e": empresa_id, "n": nome_arquivo},
    ).scalar_one()


def _inserir_lancamento(conn, empresa_id, extrato_id, data, hash_dedup) -> None:
    conn.execute(
        text(
            "INSERT INTO lancamentos (id, empresa_id, extrato_id, data, valor, descricao, "
            "tipo, hash_dedup) VALUES (gen_random_uuid(), :e, :x, :d, 10.00, 'd', "
            "'credito', :h)"
        ),
        {"e": empresa_id, "x": extrato_id, "d": data, "h": hash_dedup},
    )


def test_backfill_preenche_min_e_max_e_deixa_null_sem_lancamento(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_id = _inserir_empresa(conn)
            com_lancamentos = _inserir_extrato(conn, empresa_id, "com_lancamentos.csv")
            sem_lancamentos = _inserir_extrato(conn, empresa_id, "sem_lancamentos.csv")
            _inserir_lancamento(conn, empresa_id, com_lancamentos, "2026-09-10", "h1")
            _inserir_lancamento(conn, empresa_id, com_lancamentos, "2026-09-05", "h2")
            _inserir_lancamento(conn, empresa_id, com_lancamentos, "2026-09-20", "h3")
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            periodo_com = conn.execute(
                text("SELECT periodo_inicio, periodo_fim FROM extratos WHERE id = :x"),
                {"x": com_lancamentos},
            ).one()
            periodo_sem = conn.execute(
                text("SELECT periodo_inicio, periodo_fim FROM extratos WHERE id = :x"),
                {"x": sem_lancamentos},
            ).one()
    finally:
        engine.dispose()

    assert periodo_com[0].isoformat() == "2026-09-05"
    assert periodo_com[1].isoformat() == "2026-09-20"
    assert periodo_sem == (None, None)


def test_check_recusa_periodo_invertido_e_periodo_pela_metade(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_id = _inserir_empresa(conn)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem, periodo_inicio, periodo_fim) VALUES "
                    "(gen_random_uuid(), :e, 'a.csv', 'csv', 0, 'concluido', 'banco', "
                    "'2026-09-20', '2026-09-05')"
                ),
                {"e": empresa_id},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem, periodo_inicio, periodo_fim) VALUES "
                    "(gen_random_uuid(), :e, 'a.csv', 'csv', 0, 'concluido', 'banco', "
                    "'2026-09-05', NULL)"
                ),
                {"e": empresa_id},
            )
    finally:
        engine.dispose()
