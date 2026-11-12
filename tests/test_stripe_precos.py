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
from app.core.plano import PERIODOS, PLANOS, limite_da_conta
from app.models import Conta
from app.services.assinatura import precos as servico_precos
from main import app
from scripts import stripe_catalogo as catalogo

client = TestClient(app)

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
PRECOS_DO_ARQUIVO = json.loads(catalogo.ARQUIVO_DE_PRECOS.read_text(encoding="utf-8"))["planos"]
SEM_LIMITES = {"conciliacoes_por_mes": None, "empresas": None, "usuarios": None}
# Valores diferentes por plano e por recurso: nenhum se confunde com outro.
LIMITES_CONFIGURADOS = {
    "essencial": {"conciliacoes_por_mes": 30, "empresas": 1, "usuarios": 2},
    "padrao": {"conciliacoes_por_mes": 120, "empresas": 5, "usuarios": 6},
    "avancado": {"conciliacoes_por_mes": 600, "empresas": 20, "usuarios": 15},
}
RECURSO_DO_LIMITE = {
    "conciliacoes_por_mes": "conciliacoes",
    "empresas": "empresas",
    "usuarios": "usuarios",
}
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
            # Sem LIMITE_* na configuração, nada é limitado (issue #183).
            "limites": SEM_LIMITES,
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


# --- limites de cada plano (issue #183) ---------------------------------------------


def _configurar_limites(monkeypatch, limites: dict) -> None:
    for plano, bloco in limites.items():
        for campo, valor in bloco.items():
            monkeypatch.setattr(settings, f"limite_{RECURSO_DO_LIMITE[campo]}_{plano}", valor)


def test_limites_vazios_vem_null_nos_tres_campos_de_todos_os_itens(catalogo_da_stripe):
    corpo = _precos().json()

    assert len(corpo) == 12
    assert [p["limites"] for p in corpo] == [SEM_LIMITES] * 12


def test_cada_item_traz_o_bloco_do_seu_plano_e_os_periodos_trazem_o_mesmo(
    catalogo_da_stripe, monkeypatch
):
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)

    corpo = _precos().json()

    assert len(corpo) == 12
    for plano in PLANOS:
        do_plano = [p for p in corpo if p["plano"] == plano]
        assert [p["periodo"] for p in do_plano] == list(PERIODOS)
        # A franquia é mensal também nos planos trimestral, semestral e anual.
        assert [p["limites"] for p in do_plano] == [LIMITES_CONFIGURADOS[plano]] * 4


def test_um_plano_sem_limite_em_um_recurso_traz_null_so_nele(catalogo_da_stripe, monkeypatch):
    _configurar_limites(monkeypatch, {"padrao": {"conciliacoes_por_mes": 120, "empresas": None}})

    corpo = _precos().json()

    padrao = next(p for p in corpo if p["plano"] == "padrao")
    assert padrao["limites"] == {"conciliacoes_por_mes": 120, "empresas": None, "usuarios": None}
    essencial = next(p for p in corpo if p["plano"] == "essencial")
    assert essencial["limites"] == SEM_LIMITES


def test_o_valor_da_rota_e_o_da_regra_do_402_e_do_me(catalogo_da_stripe, monkeypatch):
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)

    corpo = _precos().json()

    for preco in corpo:
        conta = Conta(plano=preco["plano"])
        for campo, recurso in RECURSO_DO_LIMITE.items():
            assert preco["limites"][campo] == limite_da_conta(conta, recurso)


def test_o_cache_quente_nao_segura_o_limite_antigo(catalogo_da_stripe, monkeypatch):
    primeira = _precos().json()
    chamadas = len(catalogo_da_stripe.chamadas)
    assert [p["limites"] for p in primeira] == [SEM_LIMITES] * 12
    # O preço muda na Stripe e o limite muda na configuração, dentro dos 10 minutos.
    for preco in catalogo_da_stripe.precos.values():
        if preco["lookup_key"] == "padrao_mensal":
            preco["unit_amount"] = 1
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)
    _passar(monkeypatch, timedelta(minutes=9, seconds=59))

    segunda = _precos().json()

    # O limite novo aparece na hora; o preço e a Stripe seguem os do cache.
    assert len(catalogo_da_stripe.chamadas) == chamadas
    assert [p["limites"] for p in segunda] == [LIMITES_CONFIGURADOS[p["plano"]] for p in segunda]
    assert [{k: v for k, v in p.items() if k != "limites"} for p in segunda] == [
        {k: v for k, v in p.items() if k != "limites"} for p in primeira
    ]


def test_o_limite_nao_vai_para_o_cache(catalogo_da_stripe, monkeypatch):
    """Mexer na resposta não suja o que está guardado, e o limite sai da resposta
    seguinte quando a configuração volta ao vazio."""
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)
    _precos()
    assert all("limites" not in preco for preco in servico_precos._cache.precos)

    _configurar_limites(monkeypatch, {p: SEM_LIMITES for p in PLANOS})

    assert [p["limites"] for p in _precos().json()] == [SEM_LIMITES] * 12


def test_a_rota_sem_jwt_segue_200_com_limites_e_ignora_a_empresa(catalogo_da_stripe, monkeypatch):
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)

    resposta = _precos(**{"X-Ledgr-Empresa": "nao-e-um-uuid"})

    assert resposta.status_code == 200
    assert "Authorization" not in resposta.request.headers
    assert resposta.json()[0]["limites"] == LIMITES_CONFIGURADOS["essencial"]


def test_com_a_cobranca_desligada_da_503_mesmo_com_limites(catalogo_da_stripe, monkeypatch):
    _configurar_limites(monkeypatch, LIMITES_CONFIGURADOS)
    monkeypatch.setattr(settings, "stripe_habilitado", False)

    assert _precos().status_code == 503


def test_o_openapi_descreve_o_bloco_de_limites():
    esquemas = app.openapi()["components"]["schemas"]

    assert "limites" in esquemas["PrecoResponse"]["required"]
    assert set(esquemas["LimitesDoPlano"]["properties"]) == {
        "conciliacoes_por_mes",
        "empresas",
        "usuarios",
    }
