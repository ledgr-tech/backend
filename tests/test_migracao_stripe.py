"""Migração das issues #166 e #167: cobrança pela Stripe e teste no cadastro.

Sai o CHECK que exigia plano e fim do teste juntos; entram `contas.stripe_customer_id`
e as tabelas `assinaturas_stripe`, `eventos_stripe` e `pagamentos_stripe`. A conta
sem plano e sem teste ganha o teste de 14 dias; as com plano e teste ficam como
estão. O downgrade volta o CHECK, ajustando as linhas que ele recusaria.

Mesmo esquema de banco descartável de tests/test_migracao_painel_admin.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "c5f9e2b7a416"
REVISAO = "d7a4c2e9b158"
TABELAS = {"assinaturas_stripe", "eventos_stripe", "pagamentos_stripe"}
FIM = datetime(2026, 12, 1, 12, tzinfo=UTC)


def _conta(conn, cnpj: str, plano=None, teste=None) -> uuid.UUID:
    conta_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO contas (id, razao_social, cnpj, plano, teste_termina_em) "
            "VALUES (:i, 'C', :c, :p, :t)"
        ),
        {"i": conta_id, "c": cnpj, "p": plano, "t": teste},
    )
    return conta_id


def _plano_e_teste(conn, conta_id):
    return conn.execute(
        text("SELECT plano, teste_termina_em FROM contas WHERE id = :i"), {"i": conta_id}
    ).one()


@pytest.fixture
def banco(url_banco_descartavel):
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    with engine.begin() as conn:
        contas = {
            "sem_plano": _conta(conn, "11111111000191"),
            "com_teste": _conta(conn, "22222222000192", plano="padrao", teste=FIM),
        }
    yield url_banco_descartavel, engine, contas
    engine.dispose()


def test_upgrade_cria_as_tabelas_e_a_coluna_sem_o_check_e_o_downgrade_desfaz(banco):
    url, engine, contas = banco
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE contas SET teste_termina_em = NULL WHERE id = :i"),
            {"i": contas["com_teste"]},
        )

    alembic_cli(url, "upgrade", REVISAO)

    inspetor = inspect(engine)
    assert TABELAS <= set(inspetor.get_table_names())
    colunas = {c["name"]: c for c in inspetor.get_columns("contas")}
    assert colunas["stripe_customer_id"]["nullable"] is True
    checks = {c["name"] for c in inspetor.get_check_constraints("contas")}
    assert "ck_contas_plano_e_fim_do_teste_juntos" not in checks
    with engine.begin() as conn:
        # O teste agora existe sem plano, e o plano sem teste.
        conn.execute(
            text("UPDATE contas SET plano = NULL WHERE id = :i"), {"i": contas["com_teste"]}
        )
        conn.execute(
            text("UPDATE contas SET plano = 'padrao', teste_termina_em = NULL WHERE id = :i"),
            {"i": contas["com_teste"]},
        )

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    inspetor = inspect(engine)
    assert not TABELAS & set(inspetor.get_table_names())
    assert "stripe_customer_id" not in {c["name"] for c in inspetor.get_columns("contas")}
    assert "ck_contas_plano_e_fim_do_teste_juntos" in {
        c["name"] for c in inspetor.get_check_constraints("contas")
    }
    alembic_cli(url, "upgrade", REVISAO)
    assert TABELAS <= set(inspect(engine).get_table_names())


def test_a_conta_sem_plano_e_sem_teste_ganha_14_dias_e_as_outras_ficam(banco):
    url, engine, contas = banco
    antes = datetime.now(UTC)

    alembic_cli(url, "upgrade", REVISAO)

    depois = datetime.now(UTC)
    with engine.connect() as conn:
        plano, teste = _plano_e_teste(conn, contas["sem_plano"])
        assert plano is None
        assert antes + timedelta(days=14) <= teste <= depois + timedelta(days=14)
        assert _plano_e_teste(conn, contas["com_teste"]) == ("padrao", FIM)


def test_downgrade_ajusta_as_linhas_que_o_check_antigo_recusaria(banco):
    url, engine, contas = banco
    alembic_cli(url, "upgrade", REVISAO)
    with engine.begin() as conn:
        so_teste = _conta(conn, "33333333000193", teste=FIM)
        so_plano = _conta(conn, "44444444000194", plano="avancado")

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        # O teste sem plano some: a conta volta a "sem plano", como antes.
        assert _plano_e_teste(conn, so_teste) == (None, None)
        # O plano sem teste ganha um teste que já acabou.
        plano, teste = _plano_e_teste(conn, so_plano)
        assert (plano, teste is not None and teste <= datetime.now(UTC)) == ("avancado", True)
        assert _plano_e_teste(conn, contas["com_teste"]) == ("padrao", FIM)
        # A conta que ganhou o teste na subida sem plano o perde na descida.
        assert _plano_e_teste(conn, contas["sem_plano"]) == (None, None)


def _executar(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def test_restricoes_das_tabelas_novas(banco):
    url, engine, contas = banco
    alembic_cli(url, "upgrade", REVISAO)
    conta = contas["sem_plano"]
    outra = contas["com_teste"]

    # contas.stripe_customer_id é único.
    _executar(engine, "UPDATE contas SET stripe_customer_id = 'cus_1' WHERE id = :i", i=conta)
    with pytest.raises(IntegrityError):
        _executar(engine, "UPDATE contas SET stripe_customer_id = 'cus_1' WHERE id = :i", i=outra)

    assinar = (
        "INSERT INTO assinaturas_stripe (id, conta_id, stripe_subscription_id, status, plano, "
        "periodo) VALUES (:i, :c, :s, 'active', :p, :r)"
    )
    _executar(engine, assinar, i=uuid.uuid4(), c=conta, s="sub_1", p="padrao", r="mensal")
    # Uma assinatura por conta, subscription única, plano e período dentro da lista.
    for parametros in (
        {"c": conta, "s": "sub_2", "p": "padrao", "r": "mensal"},
        {"c": outra, "s": "sub_1", "p": "padrao", "r": "mensal"},
        {"c": outra, "s": "sub_3", "p": "volume", "r": "mensal"},
        {"c": outra, "s": "sub_3", "p": "padrao", "r": "diario"},
    ):
        with pytest.raises(IntegrityError):
            _executar(engine, assinar, i=uuid.uuid4(), **parametros)
    with engine.connect() as conn:
        linha = conn.execute(
            text(
                "SELECT cancelar_no_fim_do_periodo, cancelada_em, periodo_atual_fim, atualizada_em "
                "FROM assinaturas_stripe WHERE conta_id = :c"
            ),
            {"c": conta},
        ).one()
    assert linha[0] is False and linha[1] is None and linha[2] is None and linha[3] is not None

    pagar = (
        "INSERT INTO pagamentos_stripe (id, stripe_invoice_id, conta_id, valor_centavos, moeda, "
        "status, ocorrido_em) VALUES (:i, :f, :c, 100, 'brl', :s, now())"
    )
    _executar(engine, pagar, i=uuid.uuid4(), f="in_1", c=conta, s="paga")
    with pytest.raises(IntegrityError):
        _executar(engine, pagar, i=uuid.uuid4(), f="in_1", c=conta, s="paga")
    with pytest.raises(IntegrityError):
        _executar(engine, pagar, i=uuid.uuid4(), f="in_2", c=conta, s="talvez")

    _executar(engine, "INSERT INTO eventos_stripe (id, tipo) VALUES ('evt_1', 'invoice.paid')")
    with pytest.raises(IntegrityError):
        _executar(engine, "INSERT INTO eventos_stripe (id, tipo) VALUES ('evt_1', 'invoice.paid')")
    with engine.connect() as conn:
        recebido, processado = conn.execute(
            text("SELECT recebido_em, processado_em FROM eventos_stripe WHERE id = 'evt_1'")
        ).one()
    assert recebido is not None and processado is None
