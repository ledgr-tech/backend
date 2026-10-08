"""Migração da issue #28 (ADR-011): tabela `explicacoes_divergencia`.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento. Mesmo esquema de
tests/test_migracao_execucoes_conciliacao.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "a0d81fa901ae"


def _inspecionar(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        tem_tabela = "explicacoes_divergencia" in insp.get_table_names()
        indices = (
            {i["name"] for i in insp.get_indexes("explicacoes_divergencia")}
            if tem_tabela
            else set()
        )
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
    assert "ix_explicacoes_divergencia_empresa_id" in indices
    assert "ix_explicacoes_divergencia_empresa_id_criado_em" in indices
    assert "ix_explicacoes_divergencia_criado_em" in indices

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _inspecionar(url) == (False, set())

    alembic_cli(url, "upgrade", "head")
    assert _inspecionar(url)[0] is True


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    """Banco no head com uma empresa."""
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


def _inserir_explicacao(engine, empresa_id, **campos):
    valores = {
        "empresa_id": empresa_id,
        "chave": "a" * 64,
        "status": "divergente_valor",
        "provedor": "openai",
        "modelo": "gpt-6-luna",
        "versao_prompt": "v1",
        "texto": "Explicação sintética de teste.",
        "tokens_entrada": 700,
        "tokens_saida": 42,
        **campos,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO explicacoes_divergencia (id, empresa_id, chave, status, provedor, "
                "modelo, versao_prompt, texto, tokens_entrada, tokens_saida) VALUES "
                "(gen_random_uuid(), :empresa_id, :chave, :status, :provedor, :modelo, "
                ":versao_prompt, :texto, :tokens_entrada, :tokens_saida)"
            ),
            valores,
        )


def test_explicacao_valida_e_aceita(banco_migrado):
    engine, empresa_id = banco_migrado
    _inserir_explicacao(engine, empresa_id)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM explicacoes_divergencia")).scalar_one() == 1


def test_unique_empresa_id_chave_rejeita_duplicata(banco_migrado):
    engine, empresa_id = banco_migrado
    _inserir_explicacao(engine, empresa_id, chave="b" * 64)
    with pytest.raises(IntegrityError):
        _inserir_explicacao(engine, empresa_id, chave="b" * 64)


def test_mesma_chave_em_empresas_diferentes_e_aceita(banco_migrado):
    engine, empresa_id = banco_migrado
    outra_empresa_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E2', '98765432000188')"
            ),
            {"e": outra_empresa_id},
        )
    _inserir_explicacao(engine, empresa_id, chave="c" * 64)
    _inserir_explicacao(engine, outra_empresa_id, chave="c" * 64)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM explicacoes_divergencia")).scalar_one() == 2


def test_empresa_id_inexistente_rejeitado_pela_fk(banco_migrado):
    engine, _empresa_id = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir_explicacao(engine, uuid.uuid4())
