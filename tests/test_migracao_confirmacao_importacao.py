"""Migração da issue #159: quem confirmou a importação via IA e quando.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "f1c6a8e3b507"
REVISAO = "a8d2f6c4e913"


@pytest.fixture
def banco(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = create_engine(url_banco_descartavel)
    ids = {chave: uuid.uuid4() for chave in ("conta", "empresa", "usuario")}
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
                "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
                "VALUES (:u, :c, 'U', 'u@e.com', 'hash', 'admin')"
            ),
            {"u": ids["usuario"], "c": ids["conta"]},
        )
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _extrato(engine, ids, **confirmacao) -> uuid.UUID:
    extrato_id = uuid.uuid4()
    colunas = {"confirmado_por_id": None, "confirmado_por_nome": None, "confirmado_em": None}
    colunas.update(confirmacao)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, tamanho_bytes, "
                "status, origem, confirmado_por_id, confirmado_por_nome, confirmado_em) "
                "VALUES (:x, :e, 'r.json', 'json', 1, 'concluido', 'sistema', :por_id, "
                ":por_nome, CAST(:em AS timestamptz))"
            ),
            {
                "x": extrato_id,
                "e": ids["empresa"],
                "por_id": colunas["confirmado_por_id"],
                "por_nome": colunas["confirmado_por_nome"],
                "em": colunas["confirmado_em"],
            },
        )
    return extrato_id


def test_check_da_confirmacao(banco):
    _, engine, ids = banco

    _extrato(engine, ids)
    _extrato(
        engine,
        ids,
        confirmado_por_id=ids["usuario"],
        confirmado_por_nome="U",
        confirmado_em="2026-10-28T12:00:00Z",
    )
    for incompleto in (
        {"confirmado_por_nome": "U"},
        {"confirmado_em": "2026-10-28T12:00:00Z"},
        {"confirmado_por_id": ids["usuario"]},
    ):
        with pytest.raises(IntegrityError):
            _extrato(engine, ids, **incompleto)


def test_apagar_o_usuario_deixa_o_nome(banco):
    _, engine, ids = banco
    extrato_id = _extrato(
        engine,
        ids,
        confirmado_por_id=ids["usuario"],
        confirmado_por_nome="U",
        confirmado_em="2026-10-28T12:00:00Z",
    )

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usuarios WHERE id = :u"), {"u": ids["usuario"]})
        linha = conn.execute(
            text("SELECT confirmado_por_id, confirmado_por_nome FROM extratos WHERE id = :x"),
            {"x": extrato_id},
        ).one()

    assert tuple(linha) == (None, "U")


def test_downgrade_tira_as_colunas(banco):
    url, engine, _ = banco

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        colunas = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'extratos' AND column_name LIKE 'confirmado%'"
                )
            )
            .scalars()
            .all()
        )
    assert colunas == []
    alembic_cli(url, "upgrade", "head")
