"""Migração da issue #66 (ADR-012): tabela `tokens_email`.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento. Mesmo esquema de
tests/test_migracao_explicacoes_divergencia.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "dcfc210c931a"


def _inspecionar(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        tem_tabela = "tokens_email" in insp.get_table_names()
        indices = {i["name"] for i in insp.get_indexes("tokens_email")} if tem_tabela else set()
        return tem_tabela, indices
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_a_tabela(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) == (False, set())

    alembic_cli(url, "upgrade", "head")
    tem_tabela, indices = _inspecionar(url)
    assert tem_tabela is True
    assert "ix_tokens_email_usuario_id_finalidade_criado_em" in indices

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) == (False, set())


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    """Banco no head com uma empresa e um usuário."""
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    empresa_id, usuario_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', '12345678000199')"
            ),
            {"e": empresa_id},
        )
        conn.execute(
            text(
                "INSERT INTO usuarios (id, empresa_id, nome, email, senha_hash) "
                "VALUES (:u, :e, 'U', 'u@teste.com', 'hash')"
            ),
            {"u": usuario_id, "e": empresa_id},
        )
    yield engine, usuario_id
    engine.dispose()


def _inserir_token(engine, usuario_id, **campos):
    valores = {
        "usuario_id": usuario_id,
        "finalidade": "recuperacao_senha",
        "token_hash": "a" * 64,
        **campos,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tokens_email (id, usuario_id, finalidade, token_hash, expira_em) "
                "VALUES (gen_random_uuid(), :usuario_id, :finalidade, :token_hash, "
                "now() + interval '30 minutes')"
            ),
            valores,
        )


def _contar(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM tokens_email")).scalar_one()


def test_token_valido_e_aceito(banco_migrado):
    engine, usuario_id = banco_migrado
    _inserir_token(engine, usuario_id)
    assert _contar(engine) == 1


def test_token_hash_e_unico(banco_migrado):
    engine, usuario_id = banco_migrado
    _inserir_token(engine, usuario_id, token_hash="b" * 64)
    with pytest.raises(IntegrityError):
        _inserir_token(engine, usuario_id, token_hash="b" * 64)


def test_finalidade_desconhecida_e_rejeitada(banco_migrado):
    engine, usuario_id = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir_token(engine, usuario_id, finalidade="qualquer_coisa")


def test_apagar_usuario_apaga_os_tokens(banco_migrado):
    engine, usuario_id = banco_migrado
    _inserir_token(engine, usuario_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usuarios WHERE id = :u"), {"u": usuario_id})
    assert _contar(engine) == 0
