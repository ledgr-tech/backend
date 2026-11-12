"""Migração da issue #169: motivo e origem do cancelamento, `cupons_enviados` e
os tipos novos de `eventos_conta` (com `tipo` de 20 para 30 caracteres).

Mesmo esquema de banco descartável de tests/test_migracao_painel_admin.py.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError, IntegrityError

REVISAO_ANTERIOR = "d7a4c2e9b158"
REVISAO = "e8b3a6d1f294"


@pytest.fixture
def banco(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {"conta": uuid.uuid4(), "usuario": uuid.uuid4(), "assinatura": uuid.uuid4()}
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO usuarios (id, conta_id, nome, email, senha_hash, papel) "
                "VALUES (:u, :c, 'U', 'u@e.com', 'hash', 'admin')"
            ),
            {"u": ids["usuario"], "c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO assinaturas_stripe (id, conta_id, stripe_subscription_id, status, "
                "plano, periodo) VALUES (:a, :c, 'sub_1', 'active', 'padrao', 'mensal')"
            ),
            {"a": ids["assinatura"], "c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO eventos_conta (id, conta_id, tipo, motivo, origem) "
                "VALUES (:i, :c, 'inativada', 'x', 'painel')"
            ),
            {"i": uuid.uuid4(), "c": ids["conta"]},
        )
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _executar(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _evento(engine, conta, tipo: str) -> None:
    _executar(
        engine,
        "INSERT INTO eventos_conta (id, conta_id, tipo, motivo, origem) "
        "VALUES (:i, :c, :t, 'x', 'painel')",
        i=uuid.uuid4(),
        c=conta,
        t=tipo,
    )


def _cupom(engine, conta, **campos) -> None:
    dados = {
        "i": uuid.uuid4(),
        "c": conta,
        "forma": "assinatura",
        "codigo": None,
        "promo": None,
        "expira": None,
    } | campos
    _executar(
        engine,
        "INSERT INTO cupons_enviados (id, conta_id, stripe_coupon_id, cupom_nome, desconto, "
        "forma, codigo, stripe_promotion_code_id, expira_em) "
        "VALUES (:i, :c, 'cupom_20', 'Vinte', '20% por 3 meses', :forma, :codigo, :promo, :expira)",
        **dados,
    )


def test_upgrade_deixa_as_assinaturas_sem_motivo_e_aceita_so_as_origens_conhecidas(banco):
    url, engine, _ = banco

    alembic_cli(url, "upgrade", REVISAO)

    with engine.connect() as conn:
        linha = conn.execute(
            text("SELECT motivo_cancelamento, origem_cancelamento FROM assinaturas_stripe")
        ).one()
    assert tuple(linha) == (None, None)
    for origem in ("cliente", "falta_de_pagamento", "operador", "painel_stripe"):
        _executar(
            engine,
            "UPDATE assinaturas_stripe SET motivo_cancelamento = 'x', origem_cancelamento = :o",
            o=origem,
        )
    with pytest.raises(IntegrityError):
        _executar(engine, "UPDATE assinaturas_stripe SET origem_cancelamento = 'alguem'")


def test_cupons_enviados_exige_codigo_so_na_forma_codigo(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)

    _cupom(engine, ids["conta"])
    _cupom(
        engine,
        ids["conta"],
        forma="codigo",
        codigo="LEDGR-ABC",
        promo="promo_1",
        expira="2031-01-01T00:00:00Z",
    )
    invalidos = [
        {"forma": "outra"},
        {"forma": "codigo"},
        {"forma": "codigo", "codigo": "LEDGR-X", "promo": "promo_x"},
        {"forma": "assinatura", "codigo": "LEDGR-X", "promo": "promo_y"},
        {"forma": "assinatura", "expira": "2031-01-01T00:00:00Z"},
        {"forma": "codigo", "codigo": "LEDGR-Z", "promo": "promo_1", "expira": "2031-01-01Z"},
    ]
    for campos in invalidos:
        with pytest.raises((IntegrityError, DataError)):
            _cupom(engine, ids["conta"], **campos)


def test_eventos_aceitam_os_tipos_novos_e_o_tipo_cabe_em_30_caracteres(banco):
    url, engine, ids = banco
    # Antes: 21 caracteres não cabem na coluna de 20.
    with pytest.raises(DataError):
        _evento(engine, ids["conta"], "cancelamento_agendado")

    alembic_cli(url, "upgrade", REVISAO)

    for tipo in (
        "assinatura_cancelada",
        "cancelamento_agendado",
        "cancelamento_desfeito",
        "cupom_enviado",
    ):
        _evento(engine, ids["conta"], tipo)
    with pytest.raises(IntegrityError):
        _evento(engine, ids["conta"], "apagada")
    with engine.connect() as conn:
        tamanho = conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = 'eventos_conta' AND column_name = 'tipo'"
            )
        ).scalar()
    assert tamanho == 30


def test_operador_apagado_zera_o_id_e_mantem_o_nome_do_cupom(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    operador = uuid.uuid4()
    _executar(
        engine,
        "INSERT INTO operadores_ledgr (id, usuario_id, incluido_por) VALUES (:o, :u, 'R')",
        o=operador,
        u=ids["usuario"],
    )
    _cupom(engine, ids["conta"], forma="assinatura")
    _executar(
        engine, "UPDATE cupons_enviados SET operador_id = :o, operador_nome = 'Olga'", o=operador
    )

    _executar(engine, "DELETE FROM operadores_ledgr WHERE id = :o", o=operador)

    with engine.connect() as conn:
        linha = conn.execute(text("SELECT operador_id, operador_nome FROM cupons_enviados")).one()
    assert tuple(linha) == (None, "Olga")


def test_downgrade_tira_tudo_e_mantem_os_eventos_antigos_e_o_upgrade_volta(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", REVISAO)
    _cupom(engine, ids["conta"])
    _evento(engine, ids["conta"], "cancelamento_agendado")
    _evento(engine, ids["conta"], "cupom_enviado")
    _evento(engine, ids["conta"], "piloto_ligado")
    _executar(engine, "UPDATE assinaturas_stripe SET origem_cancelamento = 'operador'")

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('cupons_enviados')")).scalar() is None
        colunas = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "column_name IN ('motivo_cancelamento', 'origem_cancelamento')"
                )
            )
            .scalars()
            .all()
        )
        tipos = sorted(conn.execute(text("SELECT tipo FROM eventos_conta")).scalars().all())
        tamanho = conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = 'eventos_conta' AND column_name = 'tipo'"
            )
        ).scalar()
    assert colunas == []
    assert tipos == ["inativada", "piloto_ligado"]
    assert tamanho == 20
    with pytest.raises(IntegrityError):
        _evento(engine, ids["conta"], "cupom_enviado")
    alembic_cli(url, "upgrade", "head")
