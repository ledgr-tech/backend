"""Migração da issue #215: a tabela `consumo_empresas_apagadas`, que guarda o que
as empresas apagadas pela limpeza consumiram, por conta e mês.

Mesmo esquema de banco descartável de tests/test_migracao_remocao_empresa.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "c6a2f8e4b917"
REVISAO = "d3f7a9c2e518"
TABELA = "consumo_empresas_apagadas"
CHECK = "ck_consumo_empresas_apagadas_positivo"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior, com uma conta."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    conta_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": conta_id},
        )
    yield url_banco_descartavel, engine, conta_id
    engine.dispose()


def _inserir(engine, conta_id, mes="2026-11", extratos=1, execucoes=1):
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {TABELA} (conta_id, mes, extratos_do_banco, execucoes) "
                "VALUES (:c, :m, :x, :e)"
            ),
            {"c": conta_id, "m": mes, "x": extratos, "e": execucoes},
        )


def test_upgrade_cria_a_tabela_vazia_com_a_chave_por_conta_e_mes(banco):
    url, engine, conta_id = banco

    alembic_cli(url, "upgrade", REVISAO)

    inspetor = inspect(engine)
    assert inspetor.get_pk_constraint(TABELA)["constrained_columns"] == ["conta_id", "mes"]
    colunas = {c["name"]: c for c in inspetor.get_columns(TABELA)}
    assert set(colunas) == {"conta_id", "mes", "extratos_do_banco", "execucoes"}
    assert all(c["nullable"] is False for c in colunas.values())
    assert [fk["referred_table"] for fk in inspetor.get_foreign_keys(TABELA)] == ["contas"]
    with engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {TABELA}")) == 0
    _inserir(engine, conta_id)


def test_o_mesmo_mes_da_conta_nao_entra_duas_vezes(banco):
    url, engine, conta_id = banco
    alembic_cli(url, "upgrade", REVISAO)
    _inserir(engine, conta_id)

    with pytest.raises(IntegrityError, match="pkey"):
        _inserir(engine, conta_id)


@pytest.mark.parametrize("extratos, execucoes", [(-1, 0), (0, -1)])
def test_check_recusa_contagem_negativa(banco, extratos, execucoes):
    url, engine, conta_id = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError, match=CHECK):
        _inserir(engine, conta_id, extratos=extratos, execucoes=execucoes)


def test_fk_recusa_conta_que_nao_existe(banco):
    url, engine, _ = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError, match="fkey"):
        _inserir(engine, uuid.uuid4())


def test_downgrade_apaga_a_tabela(banco):
    url, engine, conta_id = banco
    alembic_cli(url, "upgrade", REVISAO)
    _inserir(engine, conta_id)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert TABELA not in inspect(engine).get_table_names()
    alembic_cli(url, "upgrade", "head")
