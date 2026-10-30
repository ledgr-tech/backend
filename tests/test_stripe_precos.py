"""`GET /assinatura/precos` (issues #166 e #167): preços públicos repassados da
Stripe, com cache de 10 minutos. A Stripe é a de tests/stripe_falso.py, com o
catálogo criado pelo próprio scripts/stripe_catalogo.py.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
import stripe
from fastapi.testclient import TestClient

from app.core import auth
from app.core.config import settings
from app.core.plano import PERIODOS, PLANOS
from main import app
from scripts import stripe_catalogo as catalogo

client = TestClient(app)

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
PRECOS_DO_ARQUIVO = json.loads(catalogo.ARQUIVO_DE_PRECOS.read_text(encoding="utf-8"))["planos"]
INTERVALOS = {
    "mensal": ("month", 1),
    "trimestral": ("month", 3),
    "semestral": ("month", 6),
    "anual": ("year", 1),
}


@pytest.fixture(autouse=True)
def _relogio(monkeypatch):
    monkeypatch.setattr(auth, "_agora", lambda: T0)


@pytest.fixture
def catalogo_da_stripe(stripe_falso):
    """A Stripe com os 3 produtos e os 12 preços, criados pelo script do catálogo."""
    assert catalogo.main([]) == 0
    stripe_falso.chamadas.clear()
    return stripe_falso


def _precos(**cabecalhos):
    return client.get("/assinatura/precos", headers=cabecalhos)


def _passar(monkeypatch, tempo: timedelta) -> None:
    monkeypatch.setattr(auth, "_agora", lambda: T0 + tempo)


def test_lista_completa_e_ordenada_por_plano_e_periodo(catalogo_da_stripe):
    resposta = _precos()

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert [(p["plano"], p["periodo"]) for p in corpo] == [
        (plano, periodo) for plano in PLANOS for periodo in PERIODOS
    ]
    for preco in corpo:
        intervalo, quantidade = INTERVALOS[preco["periodo"]]
        assert preco == {
            "plano": preco["plano"],
            "periodo": preco["periodo"],
            "valor_centavos": PRECOS_DO_ARQUIVO[preco["plano"]][preco["periodo"]],
            "moeda": "brl",
            "intervalo": intervalo,
            "quantidade_de_intervalos": quantidade,
            "nome_do_produto": PRECOS_DO_ARQUIVO[preco["plano"]]["nome"],
        }


def test_busca_as_12_chaves_em_chamadas_de_ate_10_com_o_produto_expandido(catalogo_da_stripe):
    _precos()

    buscas = catalogo_da_stripe.chamadas_de("Price.list")
    assert [len(b["lookup_keys"]) for b in buscas] == [10, 2]
    assert sorted(chave for b in buscas for chave in b["lookup_keys"]) == sorted(
        f"{plano}_{periodo}" for plano in PLANOS for periodo in PERIODOS
    )
    for busca in buscas:
        assert busca["active"] is True
        assert busca["expand"] == ["data.product"]
        assert busca["api_key"] == "sk_test_falsa"
        assert busca["stripe_version"] == stripe.api_version
    # Uma chamada por produto não existe: o produto vem expandido.
    assert catalogo_da_stripe.chamadas_de("Product.retrieve") == []


def test_chave_ausente_do_catalogo_simplesmente_nao_aparece(catalogo_da_stripe):
    ausente = next(
        id_ for id_, p in catalogo_da_stripe.precos.items() if p["lookup_key"] == "padrao_anual"
    )
    catalogo_da_stripe.precos[ausente]["lookup_key"] = None

    corpo = _precos().json()

    assert len(corpo) == 11
    assert ("padrao", "anual") not in [(p["plano"], p["periodo"]) for p in corpo]


def test_preco_arquivado_nao_aparece(catalogo_da_stripe):
    for preco in catalogo_da_stripe.precos.values():
        if preco["lookup_key"] == "essencial_mensal":
            preco["active"] = False

    corpo = _precos().json()

    assert ("essencial", "mensal") not in [(p["plano"], p["periodo"]) for p in corpo]


def test_catalogo_vazio_devolve_lista_vazia(stripe_falso):
    resposta = _precos()

    assert (resposta.status_code, resposta.json()) == (200, [])


def test_e_publica_sem_jwt_e_ignora_a_empresa(catalogo_da_stripe):
    resposta = _precos(**{"X-Ledgr-Empresa": "nao-e-um-uuid"})

    assert resposta.status_code == 200
    assert "Authorization" not in resposta.request.headers


def test_aparece_no_openapi_do_app_e_nao_na_api_publica():
    caminhos = app.openapi()["paths"]

    assert "get" in caminhos["/assinatura/precos"]
    assert not [c for c in caminhos if c.startswith("/v1")]


def test_o_cache_evita_a_segunda_chamada_dentro_de_10_minutos(catalogo_da_stripe, monkeypatch):
    primeira = _precos().json()
    chamadas = len(catalogo_da_stripe.chamadas)
    # Um preço muda na Stripe: dentro dos 10 minutos, ninguém vê.
    for preco in catalogo_da_stripe.precos.values():
        if preco["lookup_key"] == "padrao_mensal":
            preco["unit_amount"] = 1
    _passar(monkeypatch, timedelta(minutes=9, seconds=59))

    segunda = _precos().json()

    assert segunda == primeira
    assert len(catalogo_da_stripe.chamadas) == chamadas


def test_o_cache_expira_depois_de_10_minutos(catalogo_da_stripe, monkeypatch):
    _precos()
    for preco in catalogo_da_stripe.precos.values():
        if preco["lookup_key"] == "padrao_mensal":
            preco["unit_amount"] = 12345
    _passar(monkeypatch, timedelta(minutes=10))

    corpo = _precos().json()

    padrao_mensal = next(p for p in corpo if (p["plano"], p["periodo"]) == ("padrao", "mensal"))
    assert padrao_mensal["valor_centavos"] == 12345
    assert len(catalogo_da_stripe.chamadas_de("Price.list")) == 4  # 2 chamadas por busca


def test_sem_a_cobranca_ligada_da_503_e_nao_chama_a_stripe(catalogo_da_stripe, monkeypatch):
    _precos()
    catalogo_da_stripe.chamadas.clear()
    monkeypatch.setattr(settings, "stripe_habilitado", False)

    resposta = _precos()

    # Nem o cache vale com a cobrança desligada.
    assert resposta.status_code == 503
    assert catalogo_da_stripe.chamadas == []


def test_sem_chave_da_503(stripe_falso, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")

    assert _precos().status_code == 503
    assert stripe_falso.chamadas == []


def test_erro_da_stripe_sem_cache_da_502(catalogo_da_stripe, caplog):
    catalogo_da_stripe.erro = stripe.APIConnectionError("falha com pessoa@exemplo.com")

    resposta = _precos()

    assert resposta.status_code == 502
    assert resposta.json() == {
        "detail": "Não foi possível falar com a Stripe agora. Tente de novo em instantes."
    }
    assert "pessoa@exemplo.com" not in resposta.text + caplog.text


def test_erro_depois_do_cache_vencer_serve_o_ultimo_valor_e_loga(
    catalogo_da_stripe, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    primeira = _precos().json()
    catalogo_da_stripe.chamadas.clear()
    catalogo_da_stripe.erro = stripe.APIConnectionError("fora do ar")
    _passar(monkeypatch, timedelta(minutes=11))

    resposta = _precos()

    assert resposta.status_code == 200
    assert resposta.json() == primeira
    assert "precos resultado=cache_vencido erro=APIConnectionError" in caplog.text
    tentativas = len(catalogo_da_stripe.chamadas)

    # Não insiste a cada visita: espera 1 minuto antes de tentar de novo.
    _passar(monkeypatch, timedelta(minutes=11, seconds=30))
    assert _precos().json() == primeira
    assert len(catalogo_da_stripe.chamadas) == tentativas

    # Passado o minuto, tenta de novo e, com a Stripe de volta, atualiza.
    catalogo_da_stripe.erro = None
    _passar(monkeypatch, timedelta(minutes=12, seconds=1))
    assert _precos().status_code == 200
    assert len(catalogo_da_stripe.chamadas) > tentativas


def test_rate_limit_por_ip(catalogo_da_stripe):
    codigos = [_precos().status_code for _ in range(31)]

    assert codigos[:30] == [200] * 30
    assert codigos[30] == 429
