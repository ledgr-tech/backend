"""Migração da issue #133 (ADR-019): contas, empresas clientes e papéis.

Cada empresa vira uma conta com o mesmo id e com a razão social, o CNPJ, a
verificação, o plano, o teste e a assinatura dela; usuários e convites passam
para a conta; o administrador vira 'admin' e os demais, removidos inclusive,
'gestor'. O downgrade desfaz quando cada conta tem uma empresa só, e recusa
quando não tem ou quando um CNPJ se repete entre empresas.

Mesmo esquema de banco descartável de tests/test_migracao_plano_empresa.py.
"""

import subprocess
import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "d5a8f1c3e690"
REVISAO = "e3a7c5d9b812"

CNPJ_1 = "11222333000181"
CNPJ_2 = "12345678000195"


def _executar(url, sql, parametros=None):
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            resultado = conn.execute(text(sql), parametros or {})
            return resultado.all() if resultado.returns_rows else []
    finally:
        engine.dispose()


def _colunas(url, tabela):
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(tabela)}
    finally:
        engine.dispose()


def _dados_antigos(url):
    """Duas empresas no esquema anterior: a primeira com plano, teste,
    assinatura, verificação, um convidado ativo, um removido e um convite; a
    segunda só com o administrador."""
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    admin1, convidado, removido, admin2 = (uuid.uuid4() for _ in range(4))
    _executar(
        url,
        "INSERT INTO empresas (id, razao_social, cnpj, cnpj_verificado_em, plano, "
        "teste_termina_em, assinatura_ativa) VALUES "
        "(:e1, 'Escritório Um', :c1, '2026-10-01T12:00:00Z', 'padrao', "
        "'2026-10-15T12:00:00Z', true), "
        "(:e2, 'Empresa Dois', :c2, NULL, NULL, NULL, false)",
        {"e1": e1, "e2": e2, "c1": CNPJ_1, "c2": CNPJ_2},
    )
    _executar(
        url,
        "INSERT INTO usuarios (id, empresa_id, nome, email, senha_hash, administrador, "
        "removido_em) VALUES "
        "(:a1, :e1, 'Admin Um', 'a1@t.com', 'h', true, NULL), "
        "(:cv, :e1, 'Convidado', 'cv@t.com', 'h', false, NULL), "
        "(:rm, :e1, 'Removido', 'rm@ledgr.invalid', NULL, false, now()), "
        "(:a2, :e2, 'Admin Dois', 'a2@t.com', 'h', true, NULL)",
        {"a1": admin1, "cv": convidado, "rm": removido, "a2": admin2, "e1": e1, "e2": e2},
    )
    _executar(
        url,
        "INSERT INTO convites (id, empresa_id, email, token_hash, convidado_por_id, expira_em) "
        "VALUES (gen_random_uuid(), :e1, 'novo@t.com', :h, :a1, now() + interval '7 days')",
        {"e1": e1, "a1": admin1, "h": "a" * 64},
    )
    return {
        "e1": e1,
        "e2": e2,
        "admin1": admin1,
        "convidado": convidado,
        "removido": removido,
        "admin2": admin2,
    }


def test_upgrade_cria_conta_com_o_mesmo_id_e_downgrade_desfaz(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    ids = _dados_antigos(url)

    alembic_cli(url, "upgrade", REVISAO)

    contas = {
        linha.id: linha
        for linha in _executar(
            url,
            "SELECT id, razao_social, cnpj, cnpj_verificado_em, plano, teste_termina_em, "
            "assinatura_ativa FROM contas",
        )
    }
    assert set(contas) == {ids["e1"], ids["e2"]}
    um = contas[ids["e1"]]
    assert (um.razao_social, um.cnpj, um.plano, um.assinatura_ativa) == (
        "Escritório Um",
        CNPJ_1,
        "padrao",
        True,
    )
    assert um.cnpj_verificado_em is not None
    assert um.teste_termina_em is not None
    dois = contas[ids["e2"]]
    assert (dois.cnpj, dois.plano, dois.teste_termina_em, dois.assinatura_ativa) == (
        CNPJ_2,
        None,
        None,
        False,
    )

    empresas = _executar(url, "SELECT id, conta_id, cnpj, cnpj_verificado_em FROM empresas")
    assert {(e.id, e.conta_id) for e in empresas} == {
        (ids["e1"], ids["e1"]),
        (ids["e2"], ids["e2"]),
    }
    assert not {"plano", "teste_termina_em", "assinatura_ativa"} & _colunas(url, "empresas")

    usuarios = {
        u.id: (u.conta_id, u.papel)
        for u in _executar(url, "SELECT id, conta_id, papel FROM usuarios")
    }
    assert usuarios == {
        ids["admin1"]: (ids["e1"], "admin"),
        ids["convidado"]: (ids["e1"], "gestor"),
        ids["removido"]: (ids["e1"], "gestor"),
        ids["admin2"]: (ids["e2"], "admin"),
    }
    assert not {"empresa_id", "administrador"} & _colunas(url, "usuarios")
    assert _executar(url, "SELECT conta_id FROM convites")[0].conta_id == ids["e1"]
    assert "empresa_id" not in _colunas(url, "convites")

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert "contas" not in inspect(create_engine(url)).get_table_names()
    empresa = _executar(
        url,
        "SELECT plano, assinatura_ativa, teste_termina_em FROM empresas WHERE id = :e",
        {"e": ids["e1"]},
    )[0]
    assert (empresa.plano, empresa.assinatura_ativa) == ("padrao", True)
    assert empresa.teste_termina_em is not None
    usuarios = {
        u.id: (u.empresa_id, u.administrador)
        for u in _executar(url, "SELECT id, empresa_id, administrador FROM usuarios")
    }
    assert usuarios == {
        ids["admin1"]: (ids["e1"], True),
        ids["convidado"]: (ids["e1"], False),
        ids["removido"]: (ids["e1"], False),
        ids["admin2"]: (ids["e2"], True),
    }
    assert _executar(url, "SELECT empresa_id FROM convites")[0].empresa_id == ids["e1"]

    alembic_cli(url, "upgrade", "head")
    assert "conta_id" in _colunas(url, "empresas")


def test_downgrade_aponta_para_a_empresa_quando_o_id_difere_da_conta(url_banco_descartavel):
    """Conta criada depois da #133: a empresa tem id próprio, e o usuário volta
    para ela, não para o id da conta."""
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    conta, empresa, usuario = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _executar(
        url,
        "INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'Nova', :cnpj)",
        {"c": conta, "cnpj": CNPJ_1},
    )
    _executar(
        url,
        "INSERT INTO empresas (id, conta_id, razao_social, cnpj) VALUES (:e, :c, 'Nova', :cnpj)",
        {"e": empresa, "c": conta, "cnpj": CNPJ_1},
    )
    _executar(
        url,
        "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
        "VALUES (:u, :c, 'Admin', 'a@t.com', 'h', 'admin')",
        {"u": usuario, "c": conta},
    )

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    linha = _executar(url, "SELECT empresa_id, administrador FROM usuarios")[0]
    assert (linha.empresa_id, linha.administrador) == (empresa, True)


def _conta_com_empresas(url, cnpj_conta, *cnpjs_empresas):
    conta = uuid.uuid4()
    _executar(
        url,
        "INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'Conta', :cnpj)",
        {"c": conta, "cnpj": cnpj_conta},
    )
    for cnpj in cnpjs_empresas:
        _executar(
            url,
            "INSERT INTO empresas (id, conta_id, razao_social, cnpj) "
            "VALUES (gen_random_uuid(), :c, 'Cliente', :cnpj)",
            {"c": conta, "cnpj": cnpj},
        )
    return conta


@pytest.mark.parametrize(
    ("montar", "motivo"),
    [
        (lambda url: _conta_com_empresas(url, CNPJ_1, CNPJ_1, CNPJ_2), "sem exatamente uma"),
        (lambda url: _conta_com_empresas(url, CNPJ_1), "sem exatamente uma"),
        (
            lambda url: (
                _conta_com_empresas(url, CNPJ_1, CNPJ_2),
                _conta_com_empresas(url, CNPJ_2, CNPJ_2),
            ),
            "em mais de uma empresa",
        ),
    ],
    ids=["conta_com_duas_empresas", "conta_sem_empresa", "cnpj_repetido_entre_contas"],
)
def test_downgrade_recusa_o_que_o_esquema_anterior_nao_comporta(
    url_banco_descartavel, montar, motivo
):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    montar(url)

    with pytest.raises(subprocess.CalledProcessError) as erro:
        alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert motivo in erro.value.stderr.decode()
    assert "contas" in inspect(create_engine(url)).get_table_names()


def test_cnpj_unico_por_conta_e_um_admin_por_conta(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO)
    conta = _conta_com_empresas(url, CNPJ_1, CNPJ_2)
    # O mesmo CNPJ de empresa cliente em outra conta é aceito.
    _conta_com_empresas(url, CNPJ_2, CNPJ_2)

    with pytest.raises(IntegrityError):
        _conta_com_empresas(url, "99888777000166", CNPJ_2, CNPJ_2)
    with pytest.raises(IntegrityError):
        _conta_com_empresas(url, CNPJ_1)

    inserir = (
        "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
        "VALUES (gen_random_uuid(), :c, 'U', :email, 'h', :papel)"
    )
    _executar(url, inserir, {"c": conta, "email": "admin@t.com", "papel": "admin"})
    _executar(url, inserir, {"c": conta, "email": "gestor@t.com", "papel": "gestor"})
    _executar(url, inserir, {"c": conta, "email": "analista@t.com", "papel": "analista"})
    with pytest.raises(IntegrityError):
        _executar(url, inserir, {"c": conta, "email": "outro@t.com", "papel": "admin"})
    with pytest.raises(IntegrityError):
        _executar(url, inserir, {"c": conta, "email": "x@t.com", "papel": "administrador"})
