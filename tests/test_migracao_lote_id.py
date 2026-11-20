"""Migração da issue #237: `lote_id` em `decisoes_linha`, com o CHECK que só
o aceita na `justificada` e o índice parcial.

Mesmo esquema de banco descartável de tests/test_migracao_aprovacao_desfeita.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

REVISAO_ANTERIOR = "b9e3c7a1d548"
REVISAO = "f2c8d4a7b163"
INDICE = "ix_decisoes_linha_empresa_id_extrato_banco_id_lote_id"


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


def _inserir(engine, ids, tipo, texto=None, lote_id=None):
    colunas = ", lote_id" if lote_id is not None else ""
    valores = ", :lote" if lote_id is not None else ""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO decisoes_linha (id, empresa_id, extrato_banco_id, chave, tipo, "
                f"texto, usuario_id, autor_nome, rodada, extrato_sistema_id{colunas}) VALUES "
                f"(gen_random_uuid(), :e, :b, 'linha', :tipo, :texto, :u, 'Nome', 1, :s{valores})"
            ),
            {
                "e": ids["empresa"],
                "b": ids["banco"],
                "s": ids["sistema"],
                "u": ids["usuario"],
                "tipo": tipo,
                "texto": texto,
                "lote": lote_id,
            },
        )


def test_upgrade_cria_a_coluna_nula_o_check_e_o_indice_parcial(banco):
    url, engine, ids = banco
    _inserir(engine, ids, "justificada", "de antes")

    alembic_cli(url, "upgrade", REVISAO)

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT lote_id FROM decisoes_linha")) is None
        definicao = conn.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDICE}
        )
    assert "(empresa_id, extrato_banco_id, lote_id)" in definicao
    assert "WHERE (lote_id IS NOT NULL)" in definicao
    colunas = {c["name"]: c for c in inspect(engine).get_columns("decisoes_linha")}
    assert colunas["lote_id"]["nullable"] is True

    lote = uuid.uuid4()
    _inserir(engine, ids, "justificada", "no lote", lote_id=lote)
    _inserir(engine, ids, "justificada", "outra do lote", lote_id=lote)
    _inserir(engine, ids, "justificativa_aprovada")


@pytest.mark.parametrize(
    ("tipo", "texto"),
    [
        ("conferida", None),
        ("justificativa_aprovada", None),
        ("justificativa_rejeitada", "motivo"),
        ("justificativa_desfeita", None),
        ("conferencia_desfeita", None),
        ("aprovacao_desfeita", None),
    ],
)
def test_check_recusa_lote_id_fora_da_justificada(banco, tipo, texto):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError, match="ck_decisoes_linha_lote_so_na_justificada"):
        _inserir(engine, ids, tipo, texto, lote_id=uuid.uuid4())
    _inserir(engine, ids, tipo, texto)


def test_downgrade_remove_coluna_check_e_indice_mesmo_com_lote_gravado(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _inserir(engine, ids, "justificada", "no lote", lote_id=uuid.uuid4())

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM decisoes_linha")) == 1
        assert (
            conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": INDICE})
            == 0
        )
    with pytest.raises(ProgrammingError):
        _inserir(engine, ids, "justificada", "x", lote_id=uuid.uuid4())
    _inserir(engine, ids, "conferida")
    alembic_cli(url, "upgrade", "head")
