"""Migração da issue #122: plano, teste_termina_em e assinatura_ativa em
empresas. Empresa que já existia fica sem plano e com assinatura inativa
("sem_plano"); os CHECKs recusam plano fora da lista e plano sem fim do teste.

Mesmo esquema de banco descartável de tests/test_migracao_cnpj_verificado_em.py.
"""

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "c7d2e94a1f36"
REVISAO = "d5a8f1c3e690"
COLUNAS = {"plano", "teste_termina_em", "assinatura_ativa"}


def _colunas_empresas(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("empresas")}
    finally:
        engine.dispose()


def _inserir_empresa(conn, cnpj="12345678000195", extra_colunas="", extra_valores=""):
    return conn.execute(
        text(
            f"INSERT INTO empresas (id, razao_social, cnpj{extra_colunas}) "
            f"VALUES (gen_random_uuid(), 'E', '{cnpj}'{extra_valores}) RETURNING id"
        )
    ).scalar_one()


def test_upgrade_cria_colunas_empresa_antiga_fica_sem_plano_e_downgrade_remove(
    url_banco_descartavel,
):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert not COLUNAS & set(_colunas_empresas(url))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            antiga = _inserir_empresa(conn)
    finally:
        engine.dispose()

    alembic_cli(url, "upgrade", REVISAO)
    colunas = _colunas_empresas(url)
    assert colunas["plano"]["nullable"] is True
    assert colunas["teste_termina_em"]["nullable"] is True
    assert colunas["teste_termina_em"]["type"].timezone is True
    assert colunas["assinatura_ativa"]["nullable"] is False
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            linha = conn.execute(
                text(
                    "SELECT plano, teste_termina_em, assinatura_ativa FROM empresas WHERE id = :e"
                ),
                {"e": antiga},
            ).one()
    finally:
        engine.dispose()
    assert tuple(linha) == (None, None, False)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert not COLUNAS & set(_colunas_empresas(url))

    alembic_cli(url, "upgrade", REVISAO)
    assert COLUNAS <= set(_colunas_empresas(url))
    # Desde a issue #133, as colunas moram em `contas`.
    alembic_cli(url, "upgrade", "head")
    assert not COLUNAS & set(_colunas_empresas(url))


@pytest.mark.parametrize(
    ("colunas", "valores"),
    [
        (", plano, teste_termina_em", ", 'volume', now()"),
        (", plano", ", 'essencial'"),
        (", teste_termina_em", ", now()"),
    ],
    ids=["plano_fora_da_lista", "plano_sem_fim_do_teste", "fim_do_teste_sem_plano"],
)
def test_checks_recusam_plano_invalido_ou_pela_metade(url_banco_descartavel, colunas, valores):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _inserir_empresa(
                conn, "11222333000181", ", plano, teste_termina_em", ", 'padrao', now()"
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _inserir_empresa(conn, "12345678000195", colunas, valores)
    finally:
        engine.dispose()
