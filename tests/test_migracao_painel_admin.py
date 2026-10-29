"""Migração da issue #156: tabelas do painel administrativo, a inativação da
conta e o último login.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "b3e7d1a9c582"
REVISAO = "c5f9e2b7a416"


@pytest.fixture
def banco(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)
    engine = create_engine(url_banco_descartavel)
    ids = {"conta": uuid.uuid4(), "usuario": uuid.uuid4()}
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": ids["conta"]},
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


def _executar(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def test_inativacao_pede_data_e_motivo_juntos(banco):
    _, engine, ids = banco
    _executar(
        engine,
        "UPDATE contas SET inativada_em = now(), motivo_inativacao = 'x' WHERE id = :c",
        c=ids["conta"],
    )
    for sql in (
        "UPDATE contas SET motivo_inativacao = NULL WHERE id = :c",
        "UPDATE contas SET inativada_em = NULL WHERE id = :c",
    ):
        with pytest.raises(IntegrityError):
            _executar(engine, sql, c=ids["conta"])


def test_um_operador_ativo_por_usuario_e_os_checks(banco):
    _, engine, ids = banco
    inserir = "INSERT INTO operadores_ledgr (id, usuario_id, incluido_por) VALUES (:i, :u, 'R')"
    _executar(engine, inserir, i=uuid.uuid4(), u=ids["usuario"])
    with pytest.raises(IntegrityError):
        _executar(engine, inserir, i=uuid.uuid4(), u=ids["usuario"])
    with pytest.raises(IntegrityError):
        _executar(
            engine,
            "INSERT INTO acessos_admin (id, usuario_id, metodo, rota, resultado) "
            "VALUES (:i, :u, 'GET', '/admin/contas', 'talvez')",
            i=uuid.uuid4(),
            u=ids["usuario"],
        )
    with pytest.raises(IntegrityError):
        _executar(
            engine,
            "INSERT INTO eventos_conta (id, conta_id, tipo, motivo, origem) "
            "VALUES (:i, :c, 'apagada', 'x', 'painel')",
            i=uuid.uuid4(),
            c=ids["conta"],
        )


def test_downgrade_tira_tudo(banco):
    url, engine, _ = banco

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        for tabela in ("operadores_ledgr", "acessos_admin", "eventos_conta"):
            assert conn.execute(text(f"SELECT to_regclass('{tabela}')")).scalar() is None
        colunas = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "column_name IN ('inativada_em', 'motivo_inativacao', 'ultimo_login_em')"
                )
            )
            .scalars()
            .all()
        )
    assert colunas == []
    alembic_cli(url, "upgrade", "head")
