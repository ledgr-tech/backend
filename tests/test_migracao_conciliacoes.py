"""Migração da issue #17: coluna extratos.origem e tabela conciliacoes.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "13ddda513bcd"


def _inspecionar(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        colunas_extratos = {c["name"]: c for c in insp.get_columns("extratos")}
        tem_tabela = "conciliacoes" in insp.get_table_names()
        return colunas_extratos.get("origem"), tem_tabela
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_origem_e_conciliacoes(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) == (None, False)

    alembic_cli(url, "upgrade", "head")
    origem, tem_tabela = _inspecionar(url)
    assert tem_tabela
    assert origem is not None
    assert origem["nullable"] is False
    assert origem["default"] is None  # server_default removido depois do backfill

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) == (None, False)

    alembic_cli(url, "upgrade", "head")
    origem, tem_tabela = _inspecionar(url)
    assert origem is not None and tem_tabela


def test_extratos_existentes_viram_origem_banco_no_upgrade(url_banco_descartavel):
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
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status) VALUES (gen_random_uuid(), :e, 'a.csv', 'csv', 0, "
                    "'concluido')"
                ),
                {"e": empresa_id},
            )
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT origem FROM extratos")).scalar_one() == "banco"
    finally:
        engine.dispose()


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    """Banco no head com uma empresa, dois extratos e um lançamento."""
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("empresa", "extrato_banco", "extrato_sistema", "lancamento")}
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
        conn.execute(
            text(
                "INSERT INTO lancamentos (id, empresa_id, extrato_id, data, valor, descricao, "
                "tipo, hash_dedup) VALUES (:l, :e, :x, '2026-09-05', 10.00, 'd', 'credito', 'h')"
            ),
            {"l": ids["lancamento"], "e": ids["empresa"], "x": ids["extrato_banco"]},
        )
    yield engine, ids
    engine.dispose()


def _inserir_conciliacao(engine, ids, **campos):
    valores = {
        "empresa_id": ids["empresa"],
        "extrato_banco_id": ids["extrato_banco"],
        "extrato_sistema_id": ids["extrato_sistema"],
        "lancamento_banco_id": ids["lancamento"],
        "lancamento_sistema_id": None,
        "status": "sem_correspondencia",
        "regra_aplicada": None,
        "score_confianca": None,
        **campos,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conciliacoes (id, empresa_id, extrato_banco_id, extrato_sistema_id, "
                "lancamento_banco_id, lancamento_sistema_id, status, regra_aplicada, "
                "score_confianca) VALUES (gen_random_uuid(), :empresa_id, :extrato_banco_id, "
                ":extrato_sistema_id, :lancamento_banco_id, :lancamento_sistema_id, :status, "
                ":regra_aplicada, :score_confianca)"
            ),
            valores,
        )


def test_conciliacao_valida_e_aceita(banco_migrado):
    engine, ids = banco_migrado
    _inserir_conciliacao(
        engine,
        ids,
        lancamento_sistema_id=ids["lancamento"],
        status="match_exato",
        regra_aplicada="exato",
        score_confianca=1,
    )
    _inserir_conciliacao(engine, ids)  # só uma ponta, sem regra nem score
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM conciliacoes")).scalar_one() == 2


@pytest.mark.parametrize(
    "campos",
    [
        {"status": "inexistente"},
        {"regra_aplicada": "inexistente"},
        {"score_confianca": 1.001},
        {"score_confianca": -0.001},
        {"lancamento_banco_id": None, "lancamento_sistema_id": None},
    ],
    ids=["status", "regra_aplicada", "score_acima_de_1", "score_abaixo_de_0", "sem_lancamento"],
)
def test_checks_de_conciliacoes_rejeitam_valor_invalido(banco_migrado, campos):
    engine, ids = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir_conciliacao(engine, ids, **campos)


def test_check_de_origem_rejeita_valor_invalido(banco_migrado):
    engine, ids = banco_migrado
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("UPDATE extratos SET origem = 'outra' WHERE id = :x"),
            {"x": ids["extrato_banco"]},
        )
