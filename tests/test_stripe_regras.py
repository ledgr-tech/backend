"""Regras da cobrança pela Stripe sem rota (issues #166 e #167, ADR-026): a
configuração, a versão da API, o status da assinatura na situação da conta, o
plano efetivo e a franquia de conciliações.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import stripe
from pydantic import ValidationError
from sqlalchemy import select

from app.api.admin import _situacao
from app.core.config import Settings, settings
from app.core.plano import (
    PLANO_DOS_LIMITES_SEM_PLANO,
    PLANOS,
    guarda_arquivo_original,
    limite_da_conta,
    plano_efetivo,
    situacao_da_conta,
)
from app.models import AssinaturaStripe, Conta
from app.services.assinatura import stripe_api

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
DATABASE_URL = "postgresql+psycopg://u:p@localhost:5432/d"


def _conta(*, assinatura=None, ativa=False, plano=None, teste=None):
    return SimpleNamespace(
        assinatura_ativa=ativa, plano=plano, teste_termina_em=teste, assinatura_stripe=assinatura
    )


def _assinatura(status="active", plano="padrao"):
    return SimpleNamespace(status=status, plano=plano)


# --- configuração -------------------------------------------------------------------


def test_stripe_vem_desligada_e_sem_chaves_por_padrao():
    configuracao = Settings(database_url=DATABASE_URL, _env_file=None)

    assert configuracao.stripe_habilitado is False
    assert configuracao.stripe_secret_key == ""
    assert configuracao.stripe_webhook_secret == ""
    assert configuracao.stripe_modo_live is False
    assert configuracao.stripe_disponivel() is False
    assert configuracao.limite_conciliacoes_padrao is None


@pytest.mark.parametrize(
    ("habilitado", "chave", "disponivel"),
    [
        (False, "sk_test_x", False),
        (True, "", False),
        (True, "  ", False),
        (True, "sk_test_x", True),
    ],
)
def test_stripe_so_esta_disponivel_com_a_flag_e_a_chave(habilitado, chave, disponivel):
    configuracao = Settings(
        database_url=DATABASE_URL,
        stripe_habilitado=habilitado,
        stripe_secret_key=chave,
        _env_file=None,
    )

    assert configuracao.stripe_disponivel() is disponivel


@pytest.mark.parametrize("chave", ["sk_live_abc123", "rk_live_abc123", "  sk_live_abc123  "])
def test_chave_live_derruba_o_boot_sem_stripe_modo_live(chave):
    with pytest.raises(ValidationError) as erro:
        Settings(database_url=DATABASE_URL, stripe_secret_key=chave, _env_file=None)

    mensagem = str(erro.value)
    assert "STRIPE_MODO_LIVE" in mensagem
    # O erro do boot não imprime a chave nem o banco.
    assert "abc123" not in mensagem
    assert "u:p@localhost" not in mensagem


def test_chave_live_com_stripe_modo_live_true_passa():
    configuracao = Settings(
        database_url=DATABASE_URL,
        stripe_secret_key="sk_live_abc123",
        stripe_modo_live=True,
        _env_file=None,
    )

    assert configuracao.stripe_secret_key == "sk_live_abc123"


def test_chave_de_teste_passa_sem_stripe_modo_live():
    assert Settings(database_url=DATABASE_URL, stripe_secret_key="sk_test_x", _env_file=None)


@pytest.mark.parametrize("plano", PLANOS)
def test_limite_de_conciliacoes_vazio_e_sem_limite(plano, monkeypatch):
    monkeypatch.setenv(f"LIMITE_CONCILIACOES_{plano.upper()}", "")
    configuracao = Settings(database_url=DATABASE_URL, _env_file=None)

    assert getattr(configuracao, f"limite_conciliacoes_{plano}") is None


def test_limite_de_conciliacoes_le_o_numero(monkeypatch):
    monkeypatch.setenv("LIMITE_CONCILIACOES_PADRAO", "120")
    configuracao = Settings(database_url=DATABASE_URL, _env_file=None)

    assert configuracao.limite_conciliacoes_padrao == 120


def test_a_versao_da_api_fixada_e_a_da_biblioteca():
    """Subir a biblioteca `stripe` exige rever a versão da API junto."""
    assert stripe_api.API_VERSION == stripe.api_version


def test_lookup_key_e_plano_e_periodo():
    assert stripe_api.lookup_key("padrao", "trimestral") == "padrao_trimestral"
    assert stripe_api.plano_e_periodo("padrao_trimestral") == ("padrao", "trimestral")
    for invalida in (None, "", "padrao", "volume_mensal", "padrao_diario", "padrao_"):
        assert stripe_api.plano_e_periodo(invalida) is None


# --- status da Stripe -> situação ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "esperada"),
    [
        ("active", "ativo"),
        ("trialing", "ativo"),
        ("past_due", "pagamento_pendente"),
        ("canceled", "assinatura_encerrada"),
        ("unpaid", "assinatura_encerrada"),
        ("incomplete_expired", "assinatura_encerrada"),
        ("paused", "assinatura_encerrada"),
    ],
)
def test_cada_status_da_stripe_na_situacao_certa(status, esperada):
    conta = _conta(assinatura=_assinatura(status), teste=T0 + timedelta(days=3))

    assert situacao_da_conta(conta, T0) == esperada


def test_assinatura_incomplete_segue_pelo_teste():
    """O primeiro pagamento ainda não foi confirmado: nada é liberado nem barrado a mais."""
    incompleta = _assinatura("incomplete")

    assert situacao_da_conta(_conta(assinatura=incompleta, teste=T0 + timedelta(days=3)), T0) == (
        "teste"
    )
    assert situacao_da_conta(_conta(assinatura=incompleta, teste=T0 - timedelta(days=1)), T0) == (
        "teste_encerrado"
    )


@pytest.mark.parametrize("status", ["canceled", "past_due", "unpaid", "incomplete", "active"])
def test_piloto_e_sempre_ativo(status):
    conta = _conta(ativa=True, assinatura=_assinatura(status), teste=T0 - timedelta(days=30))

    assert situacao_da_conta(conta, T0) == "ativo"


def test_cancelamento_agendado_mantem_ativo():
    """`cancelar_no_fim_do_periodo` não muda a situação: só a Stripe encerrando a assinatura."""
    assinatura = SimpleNamespace(status="active", plano="padrao", cancelar_no_fim_do_periodo=True)

    assert situacao_da_conta(_conta(assinatura=assinatura), T0) == "ativo"


@pytest.mark.parametrize(
    ("teste", "esperada"),
    [
        (None, "sem_plano"),
        (T0 + timedelta(microseconds=1), "teste"),
        (T0, "teste_encerrado"),
    ],
)
def test_sem_assinatura_a_situacao_sai_do_teste(teste, esperada):
    assert situacao_da_conta(_conta(teste=teste), T0) == esperada


def test_o_plano_escolhido_nao_da_teste_a_conta_sem_teste():
    assert situacao_da_conta(_conta(plano="padrao"), T0) == "sem_plano"


@pytest.mark.parametrize(
    "status",
    ["active", "trialing", "past_due", "canceled", "unpaid", "incomplete", "incomplete_expired"],
)
def test_a_situacao_do_admin_em_sql_e_a_mesma_do_python(db_session, criar_empresa, status):
    conta = db_session.get(Conta, criar_empresa().conta_id)
    conta.teste_termina_em = T0 + timedelta(days=3)
    db_session.add(
        AssinaturaStripe(
            conta_id=conta.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
            status=status,
            plano="padrao",
            periodo="mensal",
        )
    )
    db_session.commit()
    db_session.refresh(conta)

    em_sql = db_session.scalar(select(_situacao(T0)).where(Conta.id == conta.id))

    assert em_sql == situacao_da_conta(conta, T0)


def test_a_situacao_do_admin_em_sql_sem_assinatura(db_session, criar_empresa):
    conta = db_session.get(Conta, criar_empresa().conta_id)
    esperado = {}
    for teste in (None, T0 + timedelta(days=3), T0 - timedelta(days=1)):
        conta.teste_termina_em = teste
        db_session.commit()
        esperado[teste] = db_session.scalar(select(_situacao(T0)).where(Conta.id == conta.id))
        db_session.refresh(conta)
        assert esperado[teste] == situacao_da_conta(conta, T0)
    assert set(esperado.values()) == {"sem_plano", "teste", "teste_encerrado"}


# --- plano efetivo ---------------------------------------------------------------------


def test_o_plano_efetivo_e_o_da_assinatura_depois_o_escolhido_depois_o_essencial():
    assert plano_efetivo(
        _conta(assinatura=_assinatura("canceled", "avancado"), plano="padrao")
    ) == ("avancado")
    assert plano_efetivo(_conta(plano="padrao")) == "padrao"
    assert plano_efetivo(_conta()) == PLANO_DOS_LIMITES_SEM_PLANO == "essencial"


def test_o_limite_de_conciliacoes_segue_o_plano_efetivo(monkeypatch):
    monkeypatch.setattr(settings, "limite_conciliacoes_essencial", 10)
    monkeypatch.setattr(settings, "limite_conciliacoes_padrao", 100)
    monkeypatch.setattr(settings, "limite_conciliacoes_avancado", None)

    assert limite_da_conta(_conta(), "conciliacoes") == 10
    assert limite_da_conta(_conta(plano="padrao"), "conciliacoes") == 100
    assert (
        limite_da_conta(
            _conta(plano="essencial", assinatura=_assinatura(plano="padrao")), ("conciliacoes")
        )
        == 100
    )
    assert limite_da_conta(_conta(assinatura=_assinatura(plano="avancado")), "conciliacoes") is None


def test_o_arquivo_original_segue_o_plano_efetivo():
    assert guarda_arquivo_original(_conta(assinatura=_assinatura(plano="avancado"))) is True
    assert guarda_arquivo_original(_conta(plano="avancado", assinatura=_assinatura())) is False
    assert guarda_arquivo_original(_conta(plano="avancado")) is True
    assert guarda_arquivo_original(_conta()) is False
