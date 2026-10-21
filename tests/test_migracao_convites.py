"""Migração da issue #69 (ADR-015): `usuarios.administrador` com backfill,
`usuarios.removido_em`, o CHECK de método de login e a tabela `convites`.

Mesmo esquema de banco descartável de tests/test_migracao_tokens_email.py.
"""

import subprocess
import uuid

import pytest
from conftest import REVISAO_ANTES_DAS_CONTAS, alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "65d4b7d91481"
UNICO_ADMINISTRADOR = "uq_usuarios_empresa_id_administrador"
INDICE_CONVITES = "ix_convites_empresa_id_criado_em"


def _estado(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        colunas = {c["name"] for c in insp.get_columns("usuarios")}
        indices = {i["name"]: i for i in insp.get_indexes("usuarios")}
        tem_convites = "convites" in insp.get_table_names()
        indices_convites = (
            {i["name"] for i in insp.get_indexes("convites")} if tem_convites else set()
        )
        return colunas, indices, tem_convites, indices_convites
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    colunas, indices, tem_convites, _ = _estado(url)
    assert {"administrador", "removido_em"} & colunas == set()
    assert UNICO_ADMINISTRADOR not in indices
    assert tem_convites is False

    alembic_cli(url, "upgrade", REVISAO_ANTES_DAS_CONTAS)
    colunas, indices, tem_convites, indices_convites = _estado(url)
    assert {"administrador", "removido_em"} <= colunas
    assert indices[UNICO_ADMINISTRADOR]["unique"]
    assert indices[UNICO_ADMINISTRADOR]["column_names"] == ["empresa_id"]
    assert tem_convites is True
    assert INDICE_CONVITES in indices_convites

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    colunas, indices, tem_convites, _ = _estado(url)
    assert {"administrador", "removido_em"} & colunas == set()
    assert tem_convites is False

    alembic_cli(url, "upgrade", "head")
    assert _estado(url)[2] is True


def _inserir_empresa(conn, cnpj: str) -> uuid.UUID:
    empresa_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', :c)"),
        {"e": empresa_id, "c": cnpj},
    )
    return empresa_id


def _inserir_usuario(conn, empresa_id, criado_em: str, **campos) -> uuid.UUID:
    usuario_id = campos.pop("id", uuid.uuid4())
    valores = {"senha_hash": "hash", "google_sub": None, **campos}
    colunas = ", ".join(valores)
    parametros = ", ".join(f":{c}" for c in valores)
    conn.execute(
        text(
            f"INSERT INTO usuarios (id, empresa_id, nome, email, criado_em, {colunas}) "
            f"VALUES (:u, :e, 'U', :email, :criado_em, {parametros})"
        ),
        {
            "u": usuario_id,
            "e": empresa_id,
            "email": f"{uuid.uuid4().hex[:12]}@teste.com",
            "criado_em": criado_em,
            **valores,
        },
    )
    return usuario_id


def test_backfill_torna_administrador_o_usuario_mais_antigo_de_cada_empresa(
    url_banco_descartavel,
):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            empresa_a = _inserir_empresa(conn, "11111111000111")
            empresa_b = _inserir_empresa(conn, "22222222000122")
            empresa_c = _inserir_empresa(conn, "33333333000133")
            a_segundo = _inserir_usuario(conn, empresa_a, "2026-09-02 10:00")
            a_primeiro = _inserir_usuario(conn, empresa_a, "2026-09-01 10:00")
            a_terceiro = _inserir_usuario(conn, empresa_a, "2026-09-03 10:00")
            b_unico = _inserir_usuario(conn, empresa_b, "2026-09-05 10:00")
            # Empate em criado_em: desempata pelo id, e sai um só.
            c_menor = _inserir_usuario(conn, empresa_c, "2026-09-07 10:00", id=uuid.UUID(int=1))
            c_maior = _inserir_usuario(conn, empresa_c, "2026-09-07 10:00", id=uuid.UUID(int=2))

        alembic_cli(url, "upgrade", REVISAO_ANTES_DAS_CONTAS)

        with engine.connect() as conn:
            administradores = dict(
                conn.execute(text("SELECT id, administrador FROM usuarios")).all()
            )
        assert administradores == {
            a_primeiro: True,
            a_segundo: False,
            a_terceiro: False,
            b_unico: True,
            c_menor: True,
            c_maior: False,
        }
    finally:
        engine.dispose()


@pytest.fixture
def banco_migrado(url_banco_descartavel):
    """Banco na revisão anterior às contas (issue #133) com uma empresa e o administrador dela."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTES_DAS_CONTAS)
    engine = create_engine(url_banco_descartavel)
    with engine.begin() as conn:
        empresa_id = _inserir_empresa(conn, "12345678000199")
        admin_id = _inserir_usuario(conn, empresa_id, "2026-09-01", administrador=True)
    yield engine, empresa_id, admin_id
    engine.dispose()


def test_segundo_administrador_na_mesma_empresa_e_rejeitado(banco_migrado):
    engine, empresa_id, _ = banco_migrado
    with engine.begin() as conn:
        _inserir_usuario(conn, empresa_id, "2026-09-02", administrador=False)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _inserir_usuario(conn, empresa_id, "2026-09-03", administrador=True)


@pytest.mark.parametrize(
    ("campos", "aceito"),
    [
        ({"senha_hash": None, "google_sub": None}, False),
        ({"senha_hash": None, "google_sub": "g-1"}, True),
        ({"senha_hash": None, "google_sub": None, "removido_em": "2026-10-13"}, True),
        ({"senha_hash": "hash", "removido_em": "2026-10-13"}, False),
        ({"senha_hash": None, "google_sub": "g-2", "removido_em": "2026-10-13"}, False),
    ],
)
def test_check_de_metodo_de_login(banco_migrado, campos, aceito):
    engine, empresa_id, _ = banco_migrado

    def inserir():
        with engine.begin() as conn:
            _inserir_usuario(conn, empresa_id, "2026-09-02", **campos)

    if aceito:
        inserir()
    else:
        with pytest.raises(IntegrityError):
            inserir()


def _inserir_convite(engine, empresa_id, convidado_por_id, token_hash="a" * 64):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO convites (id, empresa_id, email, token_hash, convidado_por_id, "
                "expira_em) VALUES (gen_random_uuid(), :e, 'x@teste.com', :h, :u, "
                "now() + interval '7 days')"
            ),
            {"e": empresa_id, "h": token_hash, "u": convidado_por_id},
        )


def test_token_hash_do_convite_e_unico(banco_migrado):
    engine, empresa_id, admin_id = banco_migrado
    _inserir_convite(engine, empresa_id, admin_id, token_hash="b" * 64)
    with pytest.raises(IntegrityError):
        _inserir_convite(engine, empresa_id, admin_id, token_hash="b" * 64)


def test_convite_nao_pode_ser_usado_e_cancelado(banco_migrado):
    engine, empresa_id, admin_id = banco_migrado
    _inserir_convite(engine, empresa_id, admin_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE convites SET usado_em = now(), cancelado_em = now()"))


def test_apagar_quem_convidou_deixa_o_convite_sem_autor(banco_migrado):
    engine, empresa_id, admin_id = banco_migrado
    _inserir_convite(engine, empresa_id, admin_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usuarios WHERE id = :u"), {"u": admin_id})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT convidado_por_id FROM convites")).scalar_one() is None


def test_downgrade_recusa_quando_ha_usuario_removido(banco_migrado):
    engine, empresa_id, _ = banco_migrado
    with engine.begin() as conn:
        _inserir_usuario(conn, empresa_id, "2026-09-02", senha_hash=None, removido_em="2026-10-13")

    with pytest.raises(subprocess.CalledProcessError) as erro:
        alembic_cli(engine.url, "downgrade", REVISAO_ANTERIOR)
    assert b"Downgrade imposs" in erro.value.stderr
