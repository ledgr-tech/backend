"""Migração da issue #158: `formato` "json" em `varchar(4)`, `status`
"rascunho", `lancamentos.posicao` e o índice dos rascunhos.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import subprocess
import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

REVISAO_ANTERIOR = "e7a3c5b9d214"
REVISAO = "f1c6a8e3b507"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior com conta e empresa."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {"conta": uuid.uuid4(), "empresa": uuid.uuid4()}
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
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _extrato(engine, ids, formato="json", status="rascunho") -> uuid.UUID:
    extrato_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, tamanho_bytes, "
                "status, origem) VALUES (:x, :e, 'r.json', :f, 1, :s, 'sistema')"
            ),
            {"x": extrato_id, "e": ids["empresa"], "f": formato, "s": status},
        )
    return extrato_id


def _lancamento(engine, ids, extrato_id, posicao):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lancamentos (id, empresa_id, extrato_id, data, valor, descricao, "
                "tipo, hash_dedup, posicao) VALUES (gen_random_uuid(), :e, :x, '2026-09-05', "
                "-1, 'd', 'debito', md5(random()::text), :p)"
            ),
            {"e": ids["empresa"], "x": extrato_id, "p": posicao},
        )


def test_antes_nao_cabe_json_nem_rascunho(banco):
    _, engine, ids = banco

    with pytest.raises(DBAPIError):
        _extrato(engine, ids, formato="json", status="concluido")
    with pytest.raises(IntegrityError):
        _extrato(engine, ids, formato="csv", status="rascunho")


def test_depois_aceita_json_rascunho_e_posicao(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    rascunho = _extrato(engine, ids)
    _lancamento(engine, ids, rascunho, 0)
    _lancamento(engine, ids, _extrato(engine, ids, formato="csv", status="concluido"), None)

    with pytest.raises(IntegrityError):
        _extrato(engine, ids, formato="pdf", status="concluido")
    with pytest.raises(IntegrityError):
        _extrato(engine, ids, formato="json", status="aprovado")
    with engine.connect() as conn:
        indice = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_extratos_rascunho_criado_em'"
            )
        ).scalar_one()
    assert "WHERE" in indice and "rascunho" in indice


def test_downgrade_sem_json_volta_o_esquema(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with pytest.raises(IntegrityError):
        _extrato(engine, ids, formato="csv", status="rascunho")
    with engine.connect() as conn:
        colunas = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'lancamentos'"
                )
            )
            .scalars()
            .all()
        )
    assert "posicao" not in colunas
    alembic_cli(url, "upgrade", "head")


def test_downgrade_recusa_extrato_json(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _extrato(engine, ids)

    with pytest.raises(subprocess.CalledProcessError) as erro:
        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert "Downgrade impossível" in erro.value.stderr.decode()
