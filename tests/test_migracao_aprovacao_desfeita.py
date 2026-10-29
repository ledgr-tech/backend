"""Migração da issue #164: tipo `aprovacao_desfeita` em `decisoes_linha`.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import subprocess
import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "d4b8f1e2a637"
REVISAO = "e7a3c5b9d214"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior com conta, empresa, usuário e o par de extratos."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("conta", "empresa", "usuario", "banco", "sistema")}
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
        for chave in ("banco", "sistema"):
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem) VALUES (:x, :e, 'a.csv', 'csv', 0, "
                    "'concluido', :o)"
                ),
                {"x": ids[chave], "e": ids["empresa"], "o": chave},
            )
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _inserir(engine, ids, tipo, texto=None):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO decisoes_linha (id, empresa_id, extrato_banco_id, chave, tipo, "
                "texto, usuario_id, autor_nome, rodada, extrato_sistema_id) VALUES "
                "(gen_random_uuid(), :e, :b, 'linha', :tipo, :texto, :u, 'Nome', 1, :s)"
            ),
            {
                "e": ids["empresa"],
                "b": ids["banco"],
                "s": ids["sistema"],
                "u": ids["usuario"],
                "tipo": tipo,
                "texto": texto,
            },
        )


def test_check_aceita_aprovacao_desfeita_sem_texto(banco):
    url, engine, ids = banco
    with pytest.raises(IntegrityError):
        _inserir(engine, ids, "aprovacao_desfeita")

    alembic_cli(url, "upgrade", REVISAO)

    _inserir(engine, ids, "justificada", "motivo")
    _inserir(engine, ids, "justificativa_aprovada")
    _inserir(engine, ids, "aprovacao_desfeita")
    with pytest.raises(IntegrityError):
        _inserir(engine, ids, "aprovacao_desfeita", "texto")


def test_downgrade_sem_reversao_volta_o_check(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with pytest.raises(IntegrityError):
        _inserir(engine, ids, "aprovacao_desfeita")
    alembic_cli(url, "upgrade", "head")


def test_downgrade_recusa_reversao_feita_depois(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _inserir(engine, ids, "aprovacao_desfeita")

    with pytest.raises(subprocess.CalledProcessError) as erro:
        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert "Downgrade impossível" in erro.value.stderr.decode()
