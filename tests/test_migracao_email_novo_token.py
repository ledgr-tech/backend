"""Migração da issue #149: `tokens_email.email_novo`, obrigatório na troca de
e-mail e nulo nas outras finalidades (CHECK `ck_tokens_email_email_novo`).

Mesmo esquema de banco descartável de tests/test_migracao_preferencias.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "e8a2c4f6b913"
REVISAO = "f9b3d5a7c124"


def _tem_coluna(url) -> bool:
    engine = create_engine(url)
    try:
        return "email_novo" in {c["name"] for c in inspect(engine).get_columns("tokens_email")}
    finally:
        engine.dispose()


def _usuario(conexao) -> uuid.UUID:
    conta_id = conexao.execute(
        text(
            "INSERT INTO contas (id, razao_social, cnpj) "
            "VALUES (:id, 'Conta', '11222333000181') RETURNING id"
        ),
        {"id": uuid.uuid4()},
    ).scalar_one()
    return conexao.execute(
        text(
            "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
            "VALUES (:id, :conta_id, 'Ana', 'ana@teste.com', 'hash', 'admin') RETURNING id"
        ),
        {"id": uuid.uuid4(), "conta_id": conta_id},
    ).scalar_one()


INSERIR = text(
    "INSERT INTO tokens_email (id, usuario_id, finalidade, token_hash, expira_em, email_novo) "
    "VALUES (:id, :usuario_id, :finalidade, :hash, now(), :email_novo)"
)


def test_upgrade_e_downgrade(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert not _tem_coluna(url)

    alembic_cli(url, "upgrade", REVISAO)
    assert _tem_coluna(url)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert not _tem_coluna(url)

    alembic_cli(url, "upgrade", "head")
    assert _tem_coluna(url)


@pytest.mark.parametrize(
    ("finalidade", "email_novo", "aceita"),
    [
        ("troca_email", "novo@teste.com", True),
        ("troca_email", None, False),
        ("recuperacao_senha", None, True),
        ("recuperacao_senha", "novo@teste.com", False),
    ],
)
def test_check_do_email_novo(url_banco_descartavel, finalidade, email_novo, aceita):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    engine = create_engine(url)
    try:
        with engine.begin() as conexao:
            usuario_id = _usuario(conexao)
        parametros = {
            "id": uuid.uuid4(),
            "usuario_id": usuario_id,
            "finalidade": finalidade,
            "hash": uuid.uuid4().hex * 2,
            "email_novo": email_novo,
        }
        if aceita:
            with engine.begin() as conexao:
                conexao.execute(INSERIR, parametros)
        else:
            with pytest.raises(IntegrityError), engine.begin() as conexao:
                conexao.execute(INSERIR, parametros)
    finally:
        engine.dispose()
