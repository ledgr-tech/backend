"""Migração da issue #184: `contas.cidade` (texto, nulo) e `contas.uf` (2
letras, nulo) com um CHECK cada um (as 27 UFs; cidade de 2 a 80 caracteres).
As contas que já existem ficam com nulo, sem backfill, e o downgrade remove as
colunas e os CHECKs sem apagar a conta.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento.
"""

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.localizacao import UFS

REVISAO_ANTERIOR = "a4d7e1b9c352"
REVISAO = "b9e3c7a1d548"
CHECKS = {"ck_contas_cidade_tamanho", "ck_contas_uf_valida"}


@pytest.fixture
def engine_da_migracao(url_banco_descartavel):
    engines = []

    def _engine():
        engine = create_engine(url_banco_descartavel)
        engines.append(engine)
        return engine

    yield _engine
    for engine in engines:
        engine.dispose()


def _contas(engine) -> tuple[dict, set[str]]:
    inspetor = inspect(engine)
    colunas = {c["name"]: c for c in inspetor.get_columns("contas")}
    checks = {c["name"] for c in inspetor.get_check_constraints("contas")}
    return colunas, checks


def _inserir(engine, cnpj: str, cidade=None, uf=None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO contas (id, razao_social, cnpj, cidade, uf) "
                "VALUES (gen_random_uuid(), 'C', :cnpj, :cidade, :uf)"
            ),
            {"cnpj": cnpj, "cidade": cidade, "uf": uf},
        )


def _inserir_antiga(engine, cnpj: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (gen_random_uuid(), 'C', :c)"),
            {"c": cnpj},
        )


def test_upgrade_cria_colunas_nulas_e_checks_sem_backfill_e_downgrade_remove(
    url_banco_descartavel, engine_da_migracao
):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    engine = engine_da_migracao()
    colunas, checks = _contas(engine)
    assert "cidade" not in colunas and "uf" not in colunas and not CHECKS & checks
    _inserir_antiga(engine, "11111111000191")

    alembic_cli(url, "upgrade", REVISAO)

    colunas, checks = _contas(engine)
    assert colunas["cidade"]["nullable"] is True
    assert colunas["uf"]["nullable"] is True
    assert colunas["uf"]["type"].length == 2
    assert CHECKS <= checks
    with engine.connect() as conn:
        antiga = conn.execute(text("SELECT cidade, uf FROM contas")).one()
    assert tuple(antiga) == (None, None)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    colunas, checks = _contas(engine)
    assert "cidade" not in colunas and "uf" not in colunas and not CHECKS & checks
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM contas")).scalar_one() == 1

    alembic_cli(url, "upgrade", "head")
    colunas, checks = _contas(engine)
    assert {"cidade", "uf"} <= set(colunas) and CHECKS <= checks


def test_conta_com_cidade_e_uf_preenchidas_sobrevive_ao_ciclo_do_downgrade(
    url_banco_descartavel, engine_da_migracao
):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    engine = engine_da_migracao()
    _inserir(engine, "22222222000191", cidade="Ribeirão Preto", uf="SP")

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT razao_social FROM contas")).scalar_one() == "C"


def test_check_aceita_cada_uma_das_27_ufs(url_banco_descartavel, engine_da_migracao):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = engine_da_migracao()

    assert len(UFS) == 27
    for indice, uf in enumerate(UFS):
        _inserir(engine, f"{indice:08d}000191", uf=uf)


@pytest.mark.parametrize("uf", ["XX", "sp", "S", "SPP", "", " S", "1A"])
def test_check_rejeita_uf_fora_da_lista(url_banco_descartavel, engine_da_migracao, uf):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = engine_da_migracao()

    with pytest.raises(DBAPIError) as erro:
        _inserir(engine, "44444444000191", uf=uf)

    # "SPP" nem chega ao CHECK: a coluna é de 2 letras, e o Postgres recusa antes.
    assert "ck_contas_uf_valida" in str(erro.value) or "value too long" in str(erro.value)


@pytest.mark.parametrize("cidade", ["A", "", "a" * 81])
def test_check_rejeita_cidade_fora_de_2_a_80_caracteres(
    url_banco_descartavel, engine_da_migracao, cidade
):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = engine_da_migracao()

    with pytest.raises(IntegrityError, match="ck_contas_cidade_tamanho"):
        _inserir(engine, "55555555000191", cidade=cidade)


@pytest.mark.parametrize("cidade", ["Ij", "a" * 80, None])
def test_check_aceita_cidade_de_2_a_80_caracteres_e_nula(
    url_banco_descartavel, engine_da_migracao, cidade
):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = engine_da_migracao()

    _inserir(engine, "66666666000191", cidade=cidade)
