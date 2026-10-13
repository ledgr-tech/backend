"""Migração da issue #75 (ADR-016): `usuarios.sessoes_validas_desde`, nula e
sem backfill.

Mesmo esquema de banco descartável de tests/test_migracao_tokens_email.py.
"""

import uuid

from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text

REVISAO_ANTERIOR = "c64488080d11"
REVISAO = "f2ffe353c4fe"


def _coluna(url):
    engine = create_engine(url)
    try:
        colunas = {c["name"]: c for c in inspect(engine).get_columns("usuarios")}
        return colunas.get("sessoes_validas_desde")
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _coluna(url) is None

    alembic_cli(url, "upgrade", REVISAO)
    coluna = _coluna(url)
    assert coluna is not None
    assert coluna["nullable"] is True
    assert coluna["type"].timezone is True

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _coluna(url) is None

    alembic_cli(url, "upgrade", "head")
    assert _coluna(url) is not None


def test_usuarios_existentes_ficam_sem_sessao_encerrada(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', :c)"),
                {"e": empresa_id, "c": "12345678000199"},
            )
            for administrador in (True, False):
                conn.execute(
                    text(
                        "INSERT INTO usuarios (id, empresa_id, nome, email, senha_hash, "
                        "administrador) VALUES (:u, :e, 'U', :email, 'hash', :a)"
                    ),
                    {
                        "u": uuid.uuid4(),
                        "e": empresa_id,
                        "email": f"{uuid.uuid4().hex[:12]}@teste.com",
                        "a": administrador,
                    },
                )

        alembic_cli(url, "upgrade", REVISAO)

        with engine.connect() as conn:
            valores = conn.execute(text("SELECT sessoes_validas_desde FROM usuarios")).scalars()
            assert list(valores) == [None, None]
    finally:
        engine.dispose()
