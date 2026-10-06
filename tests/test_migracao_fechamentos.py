"""Migração da issue #86: tabela `fechamentos`, CHECKs e índice único parcial.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "9d2d9979f63b"
UNICO = "uq_fechamentos_empresa_id_competencia_ativo"
INDICE = "ix_fechamentos_empresa_id_competencia_fechado_em"


def _indices(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        if "fechamentos" not in insp.get_table_names():
            return None
        return {i["name"]: i for i in insp.get_indexes("fechamentos")}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _indices(url) is None

    alembic_cli(url, "upgrade", "head")
    indices = _indices(url)
    assert indices[UNICO]["unique"]
    assert indices[UNICO]["column_names"] == ["empresa_id", "competencia"]
    assert indices[INDICE]["column_names"] == ["empresa_id", "competencia", "fechado_em"]

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _indices(url) is None

    alembic_cli(url, "upgrade", "head")
    assert UNICO in _indices(url)


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    empresa_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', '12345678000199')"
            ),
            {"e": empresa_id},
        )
    yield engine, empresa_id
    engine.dispose()


def _inserir(engine, empresa_id, competencia="2026-09", reaberto=False, reaberto_nome=None):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fechamentos (id, empresa_id, competencia, resumo, fechado_por_nome, "
                "reaberto_por_nome, reaberto_em) VALUES (gen_random_uuid(), :e, :c, '{}', "
                "'Maria', :rn, CASE WHEN :r THEN clock_timestamp() END)"
            ),
            {"e": empresa_id, "c": competencia, "r": reaberto, "rn": reaberto_nome},
        )


def test_um_ativo_e_um_reaberto_convivem_e_dois_ativos_nao(banco_migrado):
    engine, empresa_id = banco_migrado
    _inserir(engine, empresa_id, reaberto=True, reaberto_nome="Maria")
    _inserir(engine, empresa_id)

    with pytest.raises(IntegrityError):
        _inserir(engine, empresa_id)


@pytest.mark.parametrize(
    "campos",
    [
        {"competencia": "2026-13"},
        {"competencia": "2026-9"},
        {"reaberto": True},
        {"reaberto_nome": "Maria"},
    ],
    ids=["mes_13", "mes_sem_zero", "reaberto_sem_nome", "nome_sem_data"],
)
def test_checks_recusam(banco_migrado, campos):
    engine, empresa_id = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir(engine, empresa_id, **campos)
