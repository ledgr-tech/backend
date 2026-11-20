"""Migração da issue #215: colunas de remoção em `empresas`, o CHECK que as
mantém juntas, a FK de quem removeu e o índice parcial de `apagar_em`.

Mesmo esquema de banco descartável de tests/test_migracao_lote_id.py.
"""

import subprocess
import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "f2c8d4a7b163"
REVISAO = "c6a2f8e4b917"
CHECK = "ck_empresas_remocao_completa"
INDICE = "ix_empresas_apagar_em"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior, com uma conta, o admin e duas empresas."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("conta", "principal", "cliente", "usuario")}
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": ids["conta"]},
        )
        for chave, cnpj in (("principal", "12345678000199"), ("cliente", "98765432000110")):
            conn.execute(
                text(
                    "INSERT INTO empresas (id, conta_id, razao_social, cnpj) "
                    "VALUES (:e, :c, 'E', :cnpj)"
                ),
                {"e": ids[chave], "c": ids["conta"], "cnpj": cnpj},
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


def _remover(engine, empresa, usuario=None, por_nome="Quem removeu", dias=30, apagar_em=True):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE empresas SET removida_em = now(), removida_por_id = :u, "
                "removida_por_nome = :n, apagar_em = "
                + ("now() + make_interval(days => :d)" if apagar_em else "NULL")
                + " WHERE id = :e"
            ),
            {"e": empresa, "u": usuario, "n": por_nome, "d": dias},
        )


def test_upgrade_deixa_as_empresas_que_existem_ativas_e_cria_o_indice_parcial(banco):
    url, engine, _ = banco

    alembic_cli(url, "upgrade", REVISAO)

    with engine.connect() as conn:
        linhas = conn.execute(
            text("SELECT removida_em, removida_por_id, removida_por_nome, apagar_em FROM empresas")
        ).all()
        definicao = conn.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDICE}
        )
    assert len(linhas) == 2
    assert all(tuple(linha) == (None, None, None, None) for linha in linhas)
    assert "(apagar_em)" in definicao
    assert "WHERE (apagar_em IS NOT NULL)" in definicao
    colunas = {c["name"]: c for c in inspect(engine).get_columns("empresas")}
    for nome in ("removida_em", "removida_por_id", "removida_por_nome", "apagar_em"):
        assert colunas[nome]["nullable"] is True
    assert str(colunas["removida_em"]["type"]) == "TIMESTAMP"
    assert colunas["removida_em"]["type"].timezone is True
    assert colunas["apagar_em"]["type"].timezone is True


def test_check_aceita_ativa_e_removida_completa(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    _remover(engine, ids["cliente"], ids["usuario"])
    # Quem removeu pode sumir depois: a FK zera o id, e a linha continua válida.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usuarios WHERE id = :u"), {"u": ids["usuario"]})
        linha = conn.execute(
            text("SELECT removida_por_id, removida_por_nome FROM empresas WHERE id = :e"),
            {"e": ids["cliente"]},
        ).one()
    assert tuple(linha) == (None, "Quem removeu")


@pytest.mark.parametrize(
    "atualizacao",
    [
        "removida_em = now()",
        "apagar_em = now() + interval '30 days'",
        "removida_por_nome = 'Fulano'",
        "removida_por_id = (SELECT id FROM usuarios LIMIT 1)",
        "removida_em = now(), apagar_em = now() + interval '30 days'",
        "removida_em = now(), removida_por_nome = 'Fulano'",
        "removida_em = now(), apagar_em = now() - interval '1 day', removida_por_nome = 'F'",
        "removida_em = now(), apagar_em = now(), removida_por_nome = 'F'",
    ],
)
def test_check_recusa_remocao_pela_metade_ou_com_prazo_invertido(banco, atualizacao):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError, match=CHECK), engine.begin() as conn:
        conn.execute(
            text(f"UPDATE empresas SET {atualizacao} WHERE id = :e"), {"e": ids["cliente"]}
        )


def test_fk_de_quem_removeu_recusa_usuario_que_nao_existe(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError, match="fk_empresas_removida_por_id_usuarios"):
        _remover(engine, ids["cliente"], usuario=uuid.uuid4())


def test_downgrade_remove_colunas_check_e_indice_sem_empresa_removida(banco):
    url, engine, _ = banco
    alembic_cli(url, "upgrade", REVISAO)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    colunas = {c["name"] for c in inspect(engine).get_columns("empresas")}
    assert colunas.isdisjoint({"removida_em", "removida_por_id", "removida_por_nome", "apagar_em"})
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM empresas")) == 2
        assert (
            conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": INDICE})
            == 0
        )
        assert (
            conn.scalar(text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": CHECK})
            == 0
        )
    alembic_cli(url, "upgrade", "head")


def test_downgrade_recusa_enquanto_ha_empresa_removida(banco):
    """Sem as colunas, a removida voltaria a aparecer como ativa."""
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _remover(engine, ids["cliente"], ids["usuario"])

    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    colunas = {c["name"] for c in inspect(engine).get_columns("empresas")}
    assert "removida_em" in colunas
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE empresas SET removida_em = NULL, removida_por_id = NULL, "
                "removida_por_nome = NULL, apagar_em = NULL"
            )
        )
    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
