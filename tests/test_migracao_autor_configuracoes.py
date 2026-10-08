"""Migração da issue #85: autor da alteração e CHECK de tolerância em
configuracoes. Mesmo esquema de banco descartável de
tests/test_migracao_tolerancia_dias_default.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "77b5981fda9c"
CHECK = "ck_configuracoes_tolerancia_dias_nao_negativa"


def _estado(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        colunas = {c["name"]: c for c in insp.get_columns("configuracoes")}
        checks = {c["name"] for c in insp.get_check_constraints("configuracoes")}
        fks = {fk["name"]: fk for fk in insp.get_foreign_keys("configuracoes")}
        return colunas, checks, fks
    finally:
        engine.dispose()


def test_upgrade_cria_e_downgrade_remove(url_banco_descartavel):
    url = url_banco_descartavel

    alembic_cli(url, "upgrade", REVISAO_ANTERIOR)
    colunas, checks, _ = _estado(url)
    assert "atualizado_por_id" not in colunas and CHECK not in checks

    alembic_cli(url, "upgrade", "head")
    colunas, checks, fks = _estado(url)
    assert colunas["atualizado_por_id"]["nullable"] is True
    assert colunas["atualizado_por_nome"]["nullable"] is True
    assert CHECK in checks
    fk = fks["fk_configuracoes_atualizado_por_id_usuarios"]
    assert fk["referred_table"] == "usuarios"
    assert fk["options"].get("ondelete") == "SET NULL"
    assert str(colunas["tolerancia_dias_default"]["default"]) == "0"

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)
    colunas, checks, fks = _estado(url)
    assert "atualizado_por_id" not in colunas and "atualizado_por_nome" not in colunas
    assert CHECK not in checks and not fks.get("fk_configuracoes_atualizado_por_id_usuarios")

    alembic_cli(url, "upgrade", "head")


def test_check_recusa_tolerancia_negativa(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", "head")
    engine = create_engine(url_banco_descartavel)
    try:
        with engine.begin() as conn:
            empresa_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO empresas (id, razao_social, cnpj) VALUES (:e, 'E', '12345678000199')"
                ),
                {"e": empresa_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO configuracoes (id, empresa_id, tolerancia_dias_default) "
                    "VALUES (gen_random_uuid(), :e, -1)"
                ),
                {"e": empresa_id},
            )
    finally:
        engine.dispose()
