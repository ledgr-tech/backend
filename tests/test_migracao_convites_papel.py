"""Migração do papel no convite (issue #133, parte 2): `convites.papel`, com
CHECK em 'gestor' e 'analista'. Os convites que já existiam recebem 'gestor',
coerente com a migração das contas, que fez os convidados virarem gestores.

Mesmo esquema de banco descartável de tests/test_migracao_contas_empresas.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "e3a7c5d9b812"
REVISAO = "f4b8d6e1a923"


def _executar(url, sql, parametros=None):
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            resultado = conn.execute(text(sql), parametros or {})
            return resultado.all() if resultado.returns_rows else []
    finally:
        engine.dispose()


def _colunas_convites(url):
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("convites")}
    finally:
        engine.dispose()


def _conta_com_convite(url, papel_coluna=None):
    conta, admin = uuid.uuid4(), uuid.uuid4()
    _executar(
        url,
        "INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'Conta', :cnpj)",
        {"c": conta, "cnpj": uuid.uuid4().hex[:14].upper()},
    )
    _executar(
        url,
        "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
        "VALUES (:u, :c, 'Admin', :e, 'h', 'admin')",
        {"u": admin, "c": conta, "e": f"{admin.hex[:8]}@t.com"},
    )
    colunas = ", papel" if papel_coluna else ""
    valores = ", :papel" if papel_coluna else ""
    _executar(
        url,
        f"INSERT INTO convites (id, conta_id, email, token_hash, convidado_por_id, expira_em"
        f"{colunas}) VALUES (gen_random_uuid(), :c, :e, :h, :u, now() + interval '7 days'"
        f"{valores})",
        {
            "c": conta,
            "u": admin,
            "e": f"{uuid.uuid4().hex[:8]}@t.com",
            "h": uuid.uuid4().hex * 2,
            "papel": papel_coluna,
        },
    )
    return conta


def test_upgrade_da_gestor_aos_convites_existentes_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel
    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    assert "papel" not in _colunas_convites(url)
    conta = _conta_com_convite(url)

    alembic_cli(url, "upgrade", REVISAO)

    assert _colunas_convites(url)["papel"]["nullable"] is False
    (linha,) = _executar(url, "SELECT papel FROM convites WHERE conta_id = :c", {"c": conta})
    assert linha.papel == "gestor"

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    assert "papel" not in _colunas_convites(url)

    alembic_cli(url, "upgrade", "head")
    assert "papel" in _colunas_convites(url)


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_check_aceita_gestor_e_analista(url_banco_descartavel, papel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)

    _conta_com_convite(url_banco_descartavel, papel)


@pytest.mark.parametrize("papel", ["admin", "administrador", ""])
def test_check_recusa_admin_e_valor_fora_da_lista(url_banco_descartavel, papel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO)

    with pytest.raises(IntegrityError):
        _conta_com_convite(url_banco_descartavel, papel)
