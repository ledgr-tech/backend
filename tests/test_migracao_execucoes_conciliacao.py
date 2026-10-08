"""Migração da issue #27 (ADR-010): tabela `execucoes_conciliacao`.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento. Mesmo esquema de
tests/test_migracao_conciliacoes.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "9f2c6b4d1a77"


def _inspecionar(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return "execucoes_conciliacao" in insp.get_table_names()
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_a_tabela(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) is False

    alembic_cli(url, "upgrade", "head")
    assert _inspecionar(url) is True

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) is False

    alembic_cli(url, "upgrade", "head")
    assert _inspecionar(url) is True


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    """Banco no head com uma empresa e dois extratos (banco e sistema)."""
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("empresa", "extrato_banco", "extrato_sistema")}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', '12345678000199')"
            ),
            {"e": ids["empresa"]},
        )
        for chave, origem in (("extrato_banco", "banco"), ("extrato_sistema", "sistema")):
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem) VALUES (:x, :e, 'a.csv', 'csv', 0, "
                    "'concluido', :o)"
                ),
                {"x": ids[chave], "e": ids["empresa"], "o": origem},
            )
    yield engine, ids
    engine.dispose()


def _inserir_execucao(engine, ids, **campos):
    valores = {
        "empresa_id": ids["empresa"],
        "extrato_banco_id": ids["extrato_banco"],
        "extrato_sistema_id": ids["extrato_sistema"],
        "tolerancia_dias": 0,
        "total": 1,
        "match_exato": 1,
        "match_tolerancia": 0,
        "duplicado": 0,
        "sem_correspondencia": 0,
        "tarifa_bancaria": 0,
        "divergente_valor": 0,
        "divergente_data": 0,
        **campos,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO execucoes_conciliacao (id, empresa_id, extrato_banco_id, "
                "extrato_sistema_id, tolerancia_dias, total, match_exato, match_tolerancia, "
                "duplicado, sem_correspondencia, tarifa_bancaria, divergente_valor, "
                "divergente_data) VALUES (gen_random_uuid(), :empresa_id, :extrato_banco_id, "
                ":extrato_sistema_id, :tolerancia_dias, :total, :match_exato, "
                ":match_tolerancia, :duplicado, :sem_correspondencia, :tarifa_bancaria, "
                ":divergente_valor, :divergente_data)"
            ),
            valores,
        )


def test_execucao_valida_e_aceita(banco_migrado):
    engine, ids = banco_migrado
    _inserir_execucao(engine, ids)
    _inserir_execucao(engine, ids, total=3, match_exato=1, match_tolerancia=2)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM execucoes_conciliacao")).scalar_one() == 2


@pytest.mark.parametrize(
    "campos",
    [
        {"total": 2},  # soma das categorias é 1, não 2
        {"total": -1, "match_exato": -1},
        {"match_tolerancia": -1, "total": 0, "match_exato": 0},
    ],
    ids=["total_nao_bate_com_soma", "contagem_negativa", "match_tolerancia_negativo"],
)
def test_checks_de_execucoes_conciliacao_rejeitam_valor_invalido(banco_migrado, campos):
    engine, ids = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir_execucao(engine, ids, **campos)


def test_check_de_soma_aceita_as_7_categorias_somando_ate_total(banco_migrado):
    engine, ids = banco_migrado
    _inserir_execucao(
        engine,
        ids,
        total=7,
        match_exato=1,
        match_tolerancia=1,
        duplicado=1,
        sem_correspondencia=1,
        tarifa_bancaria=1,
        divergente_valor=1,
        divergente_data=1,
    )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT total FROM execucoes_conciliacao")).scalar_one() == 7
