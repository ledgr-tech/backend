"""Franquia de conciliações por plano (issues #166 e #167, ADR-026): quantas
primeiras conciliações de extrato do banco a conta faz por mês civil de São
Paulo, com todas as empresas somadas, e o 402 ao chegar no limite (atrás de
`PLANO_BLOQUEIO_HABILITADO`). O bloqueio por situação está em
tests/test_plano_bloqueio.py.
"""

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core import auth
from app.core.config import settings
from app.models import AssinaturaStripe, Conta, Empresa, ExecucaoConciliacao
from app.services.plano import conciliacoes_no_mes, mes_civil_de_sao_paulo
from main import app

client = TestClient(app)

LIMITE_ATINGIDO = (
    "Sua conta chegou ao limite de conciliações do mês no plano. "
    "Troque de plano para conciliar mais extratos."
)


@pytest.fixture
def conta(
    db_session,
    criar_usuario,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
):
    """Administrador com uma empresa, em teste, e fábricas de extrato e de conciliação.
    O relógio é o real; `no_instante` o congela."""
    administrador = criar_usuario()
    administrador_conta = db_session.get(Conta, administrador.conta_id)
    administrador_conta.teste_termina_em = datetime.now(UTC) + timedelta(days=30)
    db_session.commit()
    principal = empresa_unica(db_session, administrador.conta_id)

    def headers(empresa_id=None):
        # `auth_time` no relógio da aplicação, congelado ou não.
        token = gerar_token(
            administrador.conta_id,
            usuario_id=administrador.id,
            auth_time=math.floor(auth._agora().timestamp()),
        )
        return cabecalhos(token, empresa_id)

    def extrato(origem, empresa_id=None, itens=(("10.00", "a"), ("20.00", "b"))):
        novo = criar_extrato_da_empresa(empresa_id or principal, origem)
        inserir_lancamentos(novo, list(itens))
        return novo

    def conciliar(banco, sistema, empresa_id=None):
        return client.post(
            "/conciliacoes",
            headers=headers(empresa_id or principal),
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )

    def conciliar_novo(empresa_id=None):
        return conciliar(extrato("banco", empresa_id), extrato("sistema", empresa_id), empresa_id)

    def outra_empresa():
        return criar_empresa(conta_id=administrador.conta_id).id

    return SimpleNamespace(
        conta_id=administrador.conta_id,
        headers=headers,
        extrato=extrato,
        conciliar=conciliar,
        conciliar_novo=conciliar_novo,
        outra_empresa=outra_empresa,
    )


def _no_instante(monkeypatch, instante: datetime) -> None:
    monkeypatch.setattr(auth, "_agora", lambda: instante)


def _datar_execucoes(db_session, conta_id, instante: datetime) -> None:
    """Põe a data de todas as execuções da conta em `instante`."""
    db_session.execute(
        update(ExecucaoConciliacao)
        .where(
            ExecucaoConciliacao.empresa_id.in_(
                select(Empresa.id).where(Empresa.conta_id == conta_id)
            )
        )
        .values(criado_em=instante)
    )
    db_session.commit()


def _limite(monkeypatch, valor, plano="essencial") -> None:
    monkeypatch.setattr(settings, f"limite_conciliacoes_{plano}", valor)


@pytest.fixture
def bloqueio_ligado(monkeypatch):
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)


# --- o mês civil de São Paulo ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("instante", "inicio", "fim"),
    [
        (
            datetime(2026, 10, 15, 12, tzinfo=UTC),
            datetime(2026, 10, 1, 3, tzinfo=UTC),
            datetime(2026, 11, 1, 3, tzinfo=UTC),
        ),
        # 02:59 UTC de 1º/11 ainda é 23:59 de 31/10 em São Paulo.
        (
            datetime(2026, 11, 1, 2, 59, tzinfo=UTC),
            datetime(2026, 10, 1, 3, tzinfo=UTC),
            datetime(2026, 11, 1, 3, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 3, tzinfo=UTC),
            datetime(2026, 12, 1, 3, tzinfo=UTC),
        ),
        (
            datetime(2026, 12, 31, 23, tzinfo=UTC),
            datetime(2026, 12, 1, 3, tzinfo=UTC),
            datetime(2027, 1, 1, 3, tzinfo=UTC),
        ),
        # Fevereiro de ano que não é bissexto.
        (
            datetime(2027, 2, 10, tzinfo=UTC),
            datetime(2027, 2, 1, 3, tzinfo=UTC),
            datetime(2027, 3, 1, 3, tzinfo=UTC),
        ),
    ],
)
def test_mes_civil_de_sao_paulo(instante, inicio, fim):
    assert mes_civil_de_sao_paulo(instante) == (inicio, fim)


# --- a contagem -------------------------------------------------------------------------------


def test_conta_a_primeira_execucao_de_cada_extrato_do_banco(db_session, conta):
    banco = conta.extrato("banco")
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201  # reconciliar
    assert conta.conciliar_novo().status_code == 201

    assert conciliacoes_no_mes(db_session, conta.conta_id) == 2


def test_conta_todas_as_empresas_da_conta_e_so_dela(db_session, conta, criar_usuario):
    outra = conta.outra_empresa()
    assert conta.conciliar_novo().status_code == 201
    assert conta.conciliar_novo(outra).status_code == 201
    estranha = criar_usuario()
    assert estranha.conta_id != conta.conta_id

    assert conciliacoes_no_mes(db_session, conta.conta_id) == 2
    assert conciliacoes_no_mes(db_session, estranha.conta_id) == 0


def test_virada_do_mes_zera_a_contagem(db_session, conta):
    for _ in range(2):
        assert conta.conciliar_novo().status_code == 201
    # Às 23:59:59 de 31/10 em São Paulo, que já é 1º/11 em UTC.
    _datar_execucoes(db_session, conta.conta_id, datetime(2026, 11, 1, 2, 59, 59, tzinfo=UTC))

    def usadas(*args):
        return conciliacoes_no_mes(db_session, conta.conta_id, datetime(*args, tzinfo=UTC))

    assert usadas(2026, 10, 20) == 2
    assert usadas(2026, 11, 1, 2, 59, 59) == 2
    assert usadas(2026, 11, 1, 3, 0, 0) == 0
    assert usadas(2026, 11, 20) == 0


def test_a_contagem_e_da_primeira_execucao_nao_da_ultima(db_session, conta):
    """Extrato conciliado pela primeira vez em outubro e de novo em novembro conta em outubro."""
    banco = conta.extrato("banco")
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201
    primeira, segunda = db_session.scalars(
        select(ExecucaoConciliacao)
        .where(ExecucaoConciliacao.extrato_banco_id == banco.id)
        .order_by(ExecucaoConciliacao.criado_em)
    ).all()
    primeira.criado_em = datetime(2026, 10, 10, 12, tzinfo=UTC)
    segunda.criado_em = datetime(2026, 11, 10, 12, tzinfo=UTC)
    db_session.commit()

    assert conciliacoes_no_mes(db_session, conta.conta_id, datetime(2026, 10, 20, tzinfo=UTC)) == 1
    assert conciliacoes_no_mes(db_session, conta.conta_id, datetime(2026, 11, 20, tzinfo=UTC)) == 0


# --- o bloqueio ----------------------------------------------------------------------------------


def test_sem_limite_configurado_nao_barra(db_session, conta, bloqueio_ligado):
    for _ in range(4):
        assert conta.conciliar_novo().status_code == 201


def test_no_limite_barra_so_a_primeira_conciliacao_de_extrato_novo(
    db_session, conta, bloqueio_ligado, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    _limite(monkeypatch, 2)
    banco = conta.extrato("banco")
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201
    assert conta.conciliar_novo().status_code == 201

    novo = conta.extrato("banco")
    barrada = conta.conciliar(novo, conta.extrato("sistema"))
    reconciliar_o_mesmo_par = conta.conciliar(banco, conta.extrato("sistema"))

    assert barrada.status_code == 402
    assert barrada.json() == {"detail": LIMITE_ATINGIDO}
    assert reconciliar_o_mesmo_par.status_code == 201
    assert conciliacoes_no_mes(db_session, conta.conta_id) == 2
    assert "conciliacao resultado=bloqueada_pelo_plano" in caplog.text


def test_reconciliar_extrato_antigo_passa_mesmo_no_limite(db_session, conta, monkeypatch):
    banco = conta.extrato("banco")
    sistema = conta.extrato("sistema")
    assert conta.conciliar(banco, sistema).status_code == 201
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)
    _limite(monkeypatch, 1)

    assert conta.conciliar(banco, sistema).status_code == 201
    assert conta.conciliar(banco, conta.extrato("sistema")).status_code == 201
    assert conta.conciliar_novo().status_code == 402


def test_a_franquia_soma_as_empresas_da_conta(db_session, conta, bloqueio_ligado, monkeypatch):
    outra = conta.outra_empresa()
    _limite(monkeypatch, 2)
    assert conta.conciliar_novo().status_code == 201
    assert conta.conciliar_novo(outra).status_code == 201

    assert conta.conciliar_novo().status_code == 402
    assert conta.conciliar_novo(outra).status_code == 402


def test_o_mes_novo_libera_de_novo(db_session, conta, bloqueio_ligado, monkeypatch):
    _limite(monkeypatch, 1)
    assert conta.conciliar_novo().status_code == 201
    assert conta.conciliar_novo().status_code == 402
    # A primeira conciliação foi em 31/10, às 23:59 em São Paulo.
    _datar_execucoes(db_session, conta.conta_id, datetime(2026, 11, 1, 2, 59, tzinfo=UTC))

    _no_instante(monkeypatch, datetime(2026, 11, 1, 2, 59, 30, tzinfo=UTC))
    assert conta.conciliar_novo().status_code == 402
    _no_instante(monkeypatch, datetime(2026, 11, 1, 3, 0, tzinfo=UTC))
    assert conta.conciliar_novo().status_code == 201


def test_com_o_bloqueio_desligado_nada_barra(db_session, conta, monkeypatch):
    assert settings.plano_bloqueio_habilitado is False
    _limite(monkeypatch, 1)

    for _ in range(3):
        assert conta.conciliar_novo().status_code == 201


@pytest.mark.parametrize("situacao", ["teste", "ativo", "pagamento_pendente"])
def test_a_franquia_vale_em_todas_as_situacoes_com_acesso(
    db_session, conta, bloqueio_ligado, monkeypatch, situacao
):
    _limite(monkeypatch, 1)
    conta_db = db_session.get(Conta, conta.conta_id)
    if situacao == "ativo":
        conta_db.assinatura_ativa = True
    elif situacao == "pagamento_pendente":
        db_session.add(
            AssinaturaStripe(
                conta_id=conta.conta_id,
                stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                status="past_due",
                plano="essencial",
                periodo="mensal",
            )
        )
    db_session.commit()

    assert conta.conciliar_novo().status_code == 201
    barrada = conta.conciliar_novo()

    assert barrada.status_code == 402
    assert barrada.json() == {"detail": LIMITE_ATINGIDO}


def test_o_limite_e_o_do_plano_da_assinatura(db_session, conta, bloqueio_ligado, monkeypatch):
    _limite(monkeypatch, 1, "essencial")
    _limite(monkeypatch, 3, "padrao")
    db_session.add(
        AssinaturaStripe(
            conta_id=conta.conta_id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
            status="active",
            plano="padrao",
            periodo="anual",
        )
    )
    db_session.commit()

    assert [conta.conciliar_novo().status_code for _ in range(4)] == [201, 201, 201, 402]


def test_o_limite_e_o_do_plano_escolhido_sem_assinatura(
    db_session, conta, bloqueio_ligado, monkeypatch
):
    _limite(monkeypatch, 1, "essencial")
    _limite(monkeypatch, 2, "avancado")
    db_session.get(Conta, conta.conta_id).plano = "avancado"
    db_session.commit()

    assert [conta.conciliar_novo().status_code for _ in range(3)] == [201, 201, 402]


def test_assinatura_encerrada_barra_com_mensagem_propria(
    db_session, conta, bloqueio_ligado, monkeypatch
):
    banco, sistema = conta.extrato("banco"), conta.extrato("sistema")
    assert conta.conciliar(banco, sistema).status_code == 201
    db_session.add(
        AssinaturaStripe(
            conta_id=conta.conta_id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
            status="canceled",
            plano="padrao",
            periodo="mensal",
        )
    )
    db_session.commit()

    barrada = conta.conciliar_novo()

    assert barrada.status_code == 402
    assert barrada.json() == {
        "detail": "Sua assinatura foi encerrada. Assine de novo para fazer novas conciliações."
    }
    # Reconciliar extrato que já tem execução segue liberado.
    assert conta.conciliar(banco, sistema).status_code == 201


def test_a_situacao_barra_antes_da_franquia(db_session, conta, bloqueio_ligado, monkeypatch):
    _limite(monkeypatch, 1)
    db_session.get(Conta, conta.conta_id).teste_termina_em = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.commit()

    barrada = conta.conciliar_novo()

    assert barrada.status_code == 402
    assert barrada.json()["detail"].startswith("Seu período de teste acabou.")


# --- GET /me ----------------------------------------------------------------------------------------


def test_me_traz_a_franquia_no_formato_dos_outros_limites(db_session, conta, monkeypatch):
    _limite(monkeypatch, 5, "essencial")
    _limite(monkeypatch, 50, "padrao")
    assert conta.conciliar_novo().status_code == 201
    assert conta.conciliar_novo().status_code == 201

    limites = client.get("/me", headers=conta.headers()).json()["limites"]
    assert limites["conciliacoes"] == {"usadas": 2, "maximo": 5}

    db_session.add(
        AssinaturaStripe(
            conta_id=conta.conta_id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
            status="active",
            plano="padrao",
            periodo="mensal",
        )
    )
    db_session.commit()

    limites = client.get("/me", headers=conta.headers()).json()["limites"]
    assert limites["conciliacoes"] == {"usadas": 2, "maximo": 50}
    assert set(limites) == {"empresas", "usuarios", "conciliacoes"}
