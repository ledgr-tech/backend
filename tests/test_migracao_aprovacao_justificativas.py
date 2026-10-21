"""Migração da issue #132: tipos `justificativa_aprovada` e
`justificativa_rejeitada` em `decisoes_linha`, o CHECK de texto novo e as
justificativas em vigor no deploy aprovadas.

Mesmo esquema de banco descartável de tests/test_migracao_decisoes_linha.py.
"""

import subprocess
import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "f4b8d6e1a923"
REVISAO = "a6c2e8f4b157"
AUTOR = "Aprovada na atualização de 21/10"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior com conta, empresa, usuário e o par de extratos."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("conta", "empresa", "usuario", "outro", "banco", "sistema")}
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
        for chave, papel in (("usuario", "admin"), ("outro", "analista")):
            conn.execute(
                text(
                    "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
                    "VALUES (:u, :c, :n, :email, 'hash', :p)"
                ),
                {
                    "u": ids[chave],
                    "c": ids["conta"],
                    "n": chave,
                    "email": f"{chave}@e.com",
                    "p": papel,
                },
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


def _inserir(engine, ids, chave, tipo, texto=None, usuario="usuario", rodada=1):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO decisoes_linha (id, empresa_id, extrato_banco_id, chave, tipo, "
                "texto, usuario_id, autor_nome, rodada, extrato_sistema_id) VALUES "
                "(gen_random_uuid(), :e, :b, :chave, :tipo, :texto, :u, 'Nome', :rodada, :s)"
            ),
            {
                "e": ids["empresa"],
                "b": ids["banco"],
                "s": ids["sistema"],
                "u": ids[usuario],
                "chave": chave,
                "tipo": tipo,
                "texto": texto,
                "rodada": rodada,
            },
        )


def _eventos(engine, chave):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tipo, texto, usuario_id, autor_nome, rodada, extrato_sistema_id "
                "FROM decisoes_linha WHERE chave = :c ORDER BY em, id"
            ),
            {"c": chave},
        ).all()


def test_justificativa_em_vigor_fica_aprovada_e_a_desfeita_nao(banco):
    url, engine, ids = banco
    _inserir(engine, ids, "em_vigor", "justificada", "motivo", usuario="outro", rodada=2)
    _inserir(engine, ids, "desfeita", "justificada", "motivo")
    _inserir(engine, ids, "desfeita", "justificativa_desfeita")
    _inserir(engine, ids, "conferida", "conferida")
    _inserir(engine, ids, "rejustificada", "conferida")
    _inserir(engine, ids, "rejustificada", "justificada", "outro motivo")

    alembic_cli(url, "upgrade", REVISAO)

    *_, aprovacao = _eventos(engine, "em_vigor")
    assert aprovacao.tipo == "justificativa_aprovada"
    assert aprovacao.texto is None
    assert aprovacao.usuario_id == ids["outro"]  # quem justificou
    assert aprovacao.autor_nome == AUTOR
    assert (aprovacao.rodada, aprovacao.extrato_sistema_id) == (2, ids["sistema"])
    assert _eventos(engine, "rejustificada")[-1].tipo == "justificativa_aprovada"
    assert [e.tipo for e in _eventos(engine, "desfeita")] == [
        "justificada",
        "justificativa_desfeita",
    ]
    assert [e.tipo for e in _eventos(engine, "conferida")] == ["conferida"]


def test_downgrade_apaga_as_aprovacoes_da_migracao(banco):
    url, engine, ids = banco
    _inserir(engine, ids, "em_vigor", "justificada", "motivo")
    alembic_cli(url, "upgrade", REVISAO)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert [e.tipo for e in _eventos(engine, "em_vigor")] == ["justificada"]
    with pytest.raises(IntegrityError):
        _inserir(engine, ids, "nova", "justificativa_aprovada")
    alembic_cli(url, "upgrade", "head")


@pytest.mark.parametrize(
    ("tipo", "texto"),
    [("justificativa_aprovada", None), ("justificativa_rejeitada", "motivo")],
)
def test_downgrade_recusa_aprovacao_ou_rejeicao_feita_depois(banco, tipo, texto):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _inserir(engine, ids, "linha", "justificada", "motivo")
    _inserir(engine, ids, "linha", tipo, texto)

    with pytest.raises(subprocess.CalledProcessError) as erro:
        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert "Downgrade impossível" in erro.value.stderr.decode()
    assert _eventos(engine, "linha")[-1].tipo == tipo


@pytest.mark.parametrize(
    ("tipo", "texto"),
    [
        ("justificativa_aprovada", None),
        ("justificativa_rejeitada", None),
        ("justificativa_rejeitada", "motivo"),
        ("justificada", "motivo"),
        ("conferida", None),
    ],
)
def test_check_aceita(banco, tipo, texto):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    _inserir(engine, ids, "linha", tipo, texto)


@pytest.mark.parametrize(
    ("tipo", "texto"),
    [
        ("justificativa_aprovada", "texto"),
        ("justificada", None),
        ("conferida", "texto"),
        ("justificativa_desfeita", "texto"),
        ("aprovada", None),
    ],
    ids=[
        "aprovacao_com_texto",
        "justificada_sem_texto",
        "conferida_com_texto",
        "desfeita_com_texto",
        "tipo_invalido",
    ],
)
def test_check_recusa(banco, tipo, texto):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    with pytest.raises(IntegrityError):
        _inserir(engine, ids, "linha", tipo, texto)
