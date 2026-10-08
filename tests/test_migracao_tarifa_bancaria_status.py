"""Migração da issue #24: 'tarifa_bancaria' entra no CHECK de status de
conciliacoes.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento. Mesmo esquema de
tests/test_migracao_conciliacoes.py, que já cobre os outros valores do CHECK.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "8c1d5e7a9b34"


@pytest.fixture
def banco_migrado_com_conciliacao_pronta(url_banco_descartavel):
    """Banco na revisão anterior (sem 'tarifa_bancaria' no CHECK) com uma
    empresa, um extrato de banco e um lançamento — o suficiente pra inserir
    uma conciliacao só-banco em qualquer revisão."""
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url)
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
                "tipo, hash_dedup) VALUES (:l, :e, :x, '2026-09-05', 10.00, 'Tarifa', "
                "'debito', 'h')"
            ),
            {"l": ids["lancamento"], "e": ids["empresa"], "x": ids["extrato_banco"]},
        )
    yield url, engine, ids
    engine.dispose()


def _inserir_tarifa_bancaria(engine, ids) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conciliacoes (id, empresa_id, extrato_banco_id, extrato_sistema_id, "
                "lancamento_banco_id, status) VALUES (gen_random_uuid(), :empresa_id, "
                ":extrato_banco_id, :extrato_sistema_id, :lancamento_id, 'tarifa_bancaria')"
            ),
            {
                "empresa_id": ids["empresa"],
                "extrato_banco_id": ids["extrato_banco"],
                "extrato_sistema_id": ids["extrato_sistema"],
                "lancamento_id": ids["lancamento"],
            },
        )


def test_antes_do_upgrade_tarifa_bancaria_e_rejeitada_pelo_check(
    banco_migrado_com_conciliacao_pronta,
):
    _url, engine, ids = banco_migrado_com_conciliacao_pronta
    with pytest.raises(IntegrityError):
        _inserir_tarifa_bancaria(engine, ids)


def test_upgrade_passa_a_aceitar_tarifa_bancaria(banco_migrado_com_conciliacao_pronta):
    url, engine, ids = banco_migrado_com_conciliacao_pronta
    alembic_cli(url, "upgrade", "head")

    _inserir_tarifa_bancaria(engine, ids)

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT status FROM conciliacoes")).scalar_one() == "tarifa_bancaria"
        )


def test_downgrade_volta_a_rejeitar_tarifa_bancaria(banco_migrado_com_conciliacao_pronta):
    url, engine, ids = banco_migrado_com_conciliacao_pronta
    alembic_cli(url, "upgrade", "head")
    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with pytest.raises(IntegrityError):
        _inserir_tarifa_bancaria(engine, ids)

    alembic_cli(url, "upgrade", "head")
    _inserir_tarifa_bancaria(engine, ids)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT status FROM conciliacoes")).scalar_one() == "tarifa_bancaria"
        )


@pytest.mark.parametrize(
    "status_valido",
    [
        "match_exato",
        "match_tolerancia",
        "divergente_valor",
        "divergente_data",
        "sem_correspondencia",
        "duplicado",
    ],
)
def test_upgrade_continua_aceitando_os_status_anteriores(
    banco_migrado_com_conciliacao_pronta, status_valido
):
    url, engine, ids = banco_migrado_com_conciliacao_pronta
    alembic_cli(url, "upgrade", "head")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conciliacoes (id, empresa_id, extrato_banco_id, extrato_sistema_id, "
                "lancamento_banco_id, status) VALUES (gen_random_uuid(), :empresa_id, "
                ":extrato_banco_id, :extrato_sistema_id, :lancamento_id, :status)"
            ),
            {
                "empresa_id": ids["empresa"],
                "extrato_banco_id": ids["extrato_banco"],
                "extrato_sistema_id": ids["extrato_sistema"],
                "lancamento_id": ids["lancamento"],
                "status": status_valido,
            },
        )
