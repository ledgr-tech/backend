"""Migração da issue #83: tabela `decisoes_linha`, com CHECKs e índice.

Mesmo esquema de banco descartável de tests/test_migracao_execucoes_conciliacao.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "955d370e5332"
INDICE = "ix_decisoes_linha_empresa_id_extrato_banco_id_chave_em"


def _estado(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        if "decisoes_linha" not in insp.get_table_names():
            return None
        return {i["name"]: i["column_names"] for i in insp.get_indexes("decisoes_linha")}
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove_tabela_e_indice(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert _estado(url) is None

    alembic_cli(url, "upgrade", "head")
    assert _estado(url)[INDICE] == ["empresa_id", "extrato_banco_id", "chave", "em"]

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert _estado(url) is None

    alembic_cli(url, "upgrade", "head")
    assert INDICE in _estado(url)


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("empresa", "usuario", "banco", "sistema")}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', '12345678000199')"
            ),
            {"e": ids["empresa"]},
        )
        conn.execute(
            text(
                "INSERT INTO usuarios (id, empresa_id, nome, email, senha_hash) "
                "VALUES (:u, :e, 'Maria', 'm@e.com', 'hash')"
            ),
            {"u": ids["usuario"], "e": ids["empresa"]},
        )
        for chave, origem in (("banco", "banco"), ("sistema", "sistema")):
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem) VALUES (:x, :e, 'a.csv', 'csv', 0, "
                    "'concluido', :o)"
                ),
                {"x": ids[chave], "e": ids["empresa"], "o": origem},
            )
    yield engine, ids
    engine.dispose()


def _inserir(engine, ids, **campos):
    valores = {"tipo": "conferida", "texto": None, "rodada": 1, **campos}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO decisoes_linha (id, empresa_id, extrato_banco_id, chave, tipo, "
                "texto, usuario_id, autor_nome, rodada, extrato_sistema_id) VALUES "
                "(gen_random_uuid(), :e, :b, 'chave', :tipo, :texto, :u, 'Maria', :rodada, :s)"
            ),
            {
                "e": ids["empresa"],
                "b": ids["banco"],
                "u": ids["usuario"],
                "s": ids["sistema"],
                **valores,
            },
        )


def test_eventos_validos_sao_aceitos_e_em_tem_fuso(banco_migrado):
    engine, ids = banco_migrado
    _inserir(engine, ids)
    _inserir(engine, ids, tipo="justificada", texto="motivo", rodada=2)
    with engine.connect() as conn:
        em = conn.execute(text("SELECT em FROM decisoes_linha LIMIT 1")).scalar_one()
    assert em.tzinfo is not None


@pytest.mark.parametrize(
    "campos",
    [
        {"tipo": "aprovada"},
        {"tipo": "justificada", "texto": None},
        {"tipo": "conferida", "texto": "não pode"},
        {"rodada": 0},
    ],
    ids=["tipo_invalido", "justificada_sem_texto", "texto_fora_da_justificativa", "rodada_0"],
)
def test_checks_recusam_evento_invalido(banco_migrado, campos):
    engine, ids = banco_migrado
    with pytest.raises(IntegrityError):
        _inserir(engine, ids, **campos)


def test_em_e_a_hora_do_insert_e_cresce_dentro_da_mesma_transacao(banco_migrado):
    # Com now() os dois "em" seriam iguais (hora de início da transação).
    engine, ids = banco_migrado
    inserir = text(
        "INSERT INTO decisoes_linha (id, empresa_id, extrato_banco_id, chave, tipo, "
        "texto, usuario_id, autor_nome, rodada, extrato_sistema_id) VALUES "
        "(gen_random_uuid(), :e, :b, 'mesma', :tipo, NULL, :u, 'Maria', 1, :s) "
        "RETURNING em"
    )
    params = {"e": ids["empresa"], "b": ids["banco"], "u": ids["usuario"], "s": ids["sistema"]}
    with engine.begin() as conn:
        primeiro = conn.execute(inserir, {**params, "tipo": "conferida"}).scalar_one()
        segundo = conn.execute(inserir, {**params, "tipo": "conferencia_desfeita"}).scalar_one()

    assert segundo > primeiro
