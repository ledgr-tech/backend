"""Migração da issue #147: `usuarios.tutoriais_dispensados` (array de texto,
padrão vazio) e `usuarios.roteiro_dispensado` (padrão false). Os usuários
que já existem ficam com o padrão.

Mesmo esquema de banco descartável de tests/test_migracao_chaves_api.py.
"""

import uuid

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "d7f1b3c5e846"
REVISAO = "e8a2c4f6b913"
COLUNAS = {"tutoriais_dispensados", "roteiro_dispensado"}


def _colunas(url):
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("usuarios")} & COLUNAS
    finally:
        engine.dispose()


def test_upgrade_da_o_padrao_aos_usuarios_existentes_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _colunas(url) == set()
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
            usuario_id = conexao.execute(
                text(
                    "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
                    "VALUES (:id, :conta_id, 'Ana', 'ana@teste.com', 'hash', 'admin') "
                    "RETURNING id"
                ),
                {"id": uuid.uuid4(), "conta_id": conta_id},
            ).scalar_one()

        alembic_cli(url, "upgrade", REVISAO)
        assert _colunas(url) == COLUNAS
        with engine.connect() as conexao:
            linha = conexao.execute(
                text(
                    "SELECT tutoriais_dispensados, roteiro_dispensado FROM usuarios WHERE id = :id"
                ),
                {"id": usuario_id},
            ).one()
        assert linha.tutoriais_dispensados == [] and linha.roteiro_dispensado is False

        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
        assert _colunas(url) == set()

        alembic_cli(url, "upgrade", "head")
        assert _colunas(url) == COLUNAS
    finally:
        engine.dispose()
