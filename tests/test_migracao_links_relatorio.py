"""Migração da issue #165: tabela `links_relatorio` e os CHECKs dela.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "a8d2f6c4e913"
REVISAO = "b3e7d1a9c582"


@pytest.fixture
def banco(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = create_engine(url_banco_descartavel)
    ids = {chave: uuid.uuid4() for chave in ("conta", "empresa", "fechamento")}
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO empresas (id, conta_id, razao_social, cnpj) "
                "VALUES (:e, :c, 'E', '12345678000199')"
            ),
            {"e": ids["empresa"], "c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO fechamentos (id, empresa_id, competencia, resumo, fechado_por_nome) "
                "VALUES (:f, :e, '2026-09', '{}', 'U')"
            ),
            {"f": ids["fechamento"], "e": ids["empresa"]},
        )
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _link(engine, ids, **colunas):
    valores = {
        "revogado_em": None,
        "revogado_por_nome": None,
        "motivo_revogacao": None,
        "acessos": 0,
        "hash": uuid.uuid4().hex * 2,
        **colunas,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO links_relatorio (id, empresa_id, fechamento_id, prefixo, hash, "
                "criado_por_nome, expira_em, revogado_em, revogado_por_nome, motivo_revogacao, "
                "acessos) VALUES (gen_random_uuid(), :e, :f, 'abcdefgh', :hash, 'U', "
                "now() + interval '30 days', CAST(:revogado_em AS timestamptz), "
                ":revogado_por_nome, :motivo_revogacao, :acessos)"
            ),
            {"e": ids["empresa"], "f": ids["fechamento"], **valores},
        )


def test_checks(banco):
    _, engine, ids = banco
    _link(engine, ids)
    _link(
        engine,
        ids,
        revogado_em="2026-10-28T12:00:00Z",
        revogado_por_nome="U",
        motivo_revogacao="reabertura",
    )

    for invalido in (
        {"revogado_em": "2026-10-28T12:00:00Z"},
        {"motivo_revogacao": "manual"},
        {
            "revogado_em": "2026-10-28T12:00:00Z",
            "revogado_por_nome": "U",
            "motivo_revogacao": "outro",
        },
        {"acessos": -1},
    ):
        with pytest.raises(IntegrityError):
            _link(engine, ids, **invalido)
    repetido = "f" * 64
    _link(engine, ids, hash=repetido)
    with pytest.raises(IntegrityError):
        _link(engine, ids, hash=repetido)


def test_downgrade_tira_a_tabela(banco):
    url, engine, _ = banco

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('links_relatorio')")).scalar() is None
    alembic_cli(url, "upgrade", "head")
