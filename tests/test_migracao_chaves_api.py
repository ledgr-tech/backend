"""Migração da issue #135: `chaves_api` (conta, escopo, nome, prefixo, hash
único, quem criou e revogou, último uso e revogação). O token nunca é gravado.

Mesmo esquema de banco descartável de tests/test_migracao_assistente.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "c3e9a1d5f724"
REVISAO = "d7f1b3c5e846"
COLUNAS = {
    "id",
    "conta_id",
    "empresa_id",
    "nome",
    "prefixo",
    "hash",
    "criada_por_id",
    "criado_em",
    "ultimo_uso_em",
    "revogada_em",
    "revogada_por_id",
}


def _colunas(url):
    engine = create_engine(url)
    try:
        inspecao = inspect(engine)
        if "chaves_api" not in inspecao.get_table_names():
            return None
        return {c["name"] for c in inspecao.get_columns("chaves_api")}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_a_tabela(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _colunas(url) is None

    alembic_cli(url, "upgrade", REVISAO)
    assert _colunas(url) == COLUNAS

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _colunas(url) is None

    alembic_cli(url, "upgrade", "head")
    assert _colunas(url) == COLUNAS


def test_hash_e_unico(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    engine = create_engine(url)
    try:
        with engine.begin() as conexao:
            conta_id = conexao.execute(
                text(
                    "INSERT INTO contas (id, razao_social, cnpj) "
                    "VALUES (:id, 'Conta', '11222333000181') RETURNING id"
                ),
                {"id": uuid.uuid4()},
            ).scalar_one()
        inserir = text(
            "INSERT INTO chaves_api (id, conta_id, nome, prefixo, hash) "
            "VALUES (:id, :conta_id, 'Chave', 'abcdefgh', :hash)"
        )
        with engine.begin() as conexao:
            conexao.execute(inserir, {"id": uuid.uuid4(), "conta_id": conta_id, "hash": "a" * 64})
        with pytest.raises(IntegrityError), engine.begin() as conexao:
            conexao.execute(inserir, {"id": uuid.uuid4(), "conta_id": conta_id, "hash": "a" * 64})
    finally:
        engine.dispose()
