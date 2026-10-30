"""Script do catálogo da Stripe (issues #166 e #167): idempotente, preço novo
quando o valor muda e `--dry-run` sem criação. A Stripe é a de
tests/stripe_falso.py.
"""

import json

import pytest

from app.core.config import settings
from app.core.plano import PERIODOS, PLANOS
from scripts import stripe_catalogo as script

CRIACOES = {"Product.create", "Product.modify", "Price.create", "Price.modify"}


def _rodar(*argumentos: str) -> int:
    return script.main(list(argumentos))


def _escritas(stripe_falso) -> list[str]:
    return [nome for nome in stripe_falso.nomes() if nome in CRIACOES]


def _arquivo(tmp_path, alteracoes: dict[tuple[str, str], int] | None = None):
    dados = json.loads(script.ARQUIVO_DE_PRECOS.read_text(encoding="utf-8"))
    for (plano, periodo), valor in (alteracoes or {}).items():
        dados["planos"][plano][periodo] = valor
    caminho = tmp_path / "precos.json"
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return caminho


def test_o_arquivo_de_precos_versionado_tem_todos_os_planos_e_esta_marcado_provisorio():
    dados = json.loads(script.ARQUIVO_DE_PRECOS.read_text(encoding="utf-8"))

    assert dados["provisorio"] is True
    assert "PROVISÓRIOS" in dados["_aviso"]
    precos = script.carregar_precos(script.ARQUIVO_DE_PRECOS)
    assert precos["moeda"] == "brl"
    assert set(precos["planos"]) == set(PLANOS)
    for plano in PLANOS:
        assert set(PERIODOS) <= set(precos["planos"][plano])


@pytest.mark.parametrize("valor", [0, -1, 100.5, "9900", True, None])
def test_valor_invalido_no_arquivo_e_recusado(tmp_path, valor):
    arquivo = _arquivo(tmp_path, {("padrao", "mensal"): valor})

    with pytest.raises(ValueError, match="padrao/mensal"):
        script.carregar_precos(arquivo)


def test_primeira_execucao_cria_um_produto_por_plano_e_um_preco_por_periodo(stripe_falso, capsys):
    assert _rodar() == 0

    assert sorted(stripe_falso.produtos) == ["ledgr_avancado", "ledgr_essencial", "ledgr_padrao"]
    produto = stripe_falso.produtos["ledgr_padrao"]
    assert produto["metadata"] == {"plano": "padrao"}
    precos = {p["lookup_key"]: p for p in stripe_falso.precos.values()}
    assert sorted(precos) == sorted(
        f"{plano}_{periodo}" for plano in PLANOS for periodo in PERIODOS
    )
    assert precos["padrao_mensal"]["recurring"] == {"interval": "month", "interval_count": 1}
    assert precos["padrao_trimestral"]["recurring"] == {"interval": "month", "interval_count": 3}
    assert precos["padrao_semestral"]["recurring"] == {"interval": "month", "interval_count": 6}
    assert precos["padrao_anual"]["recurring"] == {"interval": "year", "interval_count": 1}
    dados = script.carregar_precos(script.ARQUIVO_DE_PRECOS)
    for chave, preco in precos.items():
        plano, periodo = chave.split("_")
        assert preco["unit_amount"] == dados["planos"][plano][periodo]
        assert preco["currency"] == "brl"
        assert preco["product"] == f"ledgr_{plano}"
        assert preco["active"] is True
    assert "Modo da chave: teste" in capsys.readouterr().out


def test_usa_a_chave_e_a_versao_da_api_fixada(stripe_falso):
    _rodar()

    for _, parametros in stripe_falso.chamadas:
        assert parametros["api_key"] == "sk_test_falsa"
        assert parametros["stripe_version"] == "2026-10-28.endive"


def test_rodar_duas_vezes_sem_mudanca_nao_cria_nada(stripe_falso, capsys):
    _rodar()
    produtos, precos = dict(stripe_falso.produtos), dict(stripe_falso.precos)
    stripe_falso.chamadas.clear()

    assert _rodar() == 0

    assert _escritas(stripe_falso) == []
    assert stripe_falso.produtos == produtos
    assert stripe_falso.precos == precos
    saida = capsys.readouterr().out
    assert "criar" not in saida.split("Modo da chave")[-1]


def test_valor_novo_cria_preco_novo_transfere_a_chave_e_arquiva_o_antigo(stripe_falso, tmp_path):
    _rodar()
    antigo = next(p for p in stripe_falso.precos.values() if p["lookup_key"] == "padrao_trimestral")
    intactos = {id_: dict(p) for id_, p in stripe_falso.precos.items() if id_ != antigo["id"]}
    stripe_falso.chamadas.clear()

    assert _rodar("--arquivo", str(_arquivo(tmp_path, {("padrao", "trimestral"): 80000}))) == 0

    (criacao,) = stripe_falso.chamadas_de("Price.create")
    assert criacao["lookup_key"] == "padrao_trimestral"
    assert criacao["transfer_lookup_key"] is True
    assert criacao["unit_amount"] == 80000
    assert criacao["product"] == "ledgr_padrao"
    assert criacao["recurring"] == {"interval": "month", "interval_count": 3}
    (arquivamento,) = stripe_falso.chamadas_de("Price.modify")
    assert arquivamento == {
        "id": antigo["id"],
        "active": False,
        "api_key": "sk_test_falsa",
        "stripe_version": "2026-10-28.endive",
    }
    assert stripe_falso.precos[antigo["id"]]["active"] is False
    assert stripe_falso.precos[antigo["id"]]["lookup_key"] is None
    ativos = [p for p in stripe_falso.precos.values() if p["lookup_key"] == "padrao_trimestral"]
    assert [p["unit_amount"] for p in ativos] == [80000]
    # Nenhum outro preço foi tocado.
    assert {
        id_: p
        for id_, p in stripe_falso.precos.items()
        if id_ != antigo["id"] and p["lookup_key"] != "padrao_trimestral"
    } == intactos
    assert stripe_falso.chamadas_de("Product.create") == []


def test_depois_da_troca_de_valor_a_segunda_execucao_nao_cria_nada(stripe_falso, tmp_path):
    arquivo = _arquivo(tmp_path, {("avancado", "anual"): 1})
    _rodar()
    _rodar("--arquivo", str(arquivo))
    stripe_falso.chamadas.clear()

    _rodar("--arquivo", str(arquivo))

    assert _escritas(stripe_falso) == []


def test_dry_run_mostra_o_que_faria_e_nao_chama_nenhuma_criacao(stripe_falso, capsys):
    assert _rodar("--dry-run") == 0

    assert _escritas(stripe_falso) == []
    assert stripe_falso.produtos == {} and stripe_falso.precos == {}
    saida = capsys.readouterr().out
    assert "(simulação)" in saida
    assert "produto ledgr_padrao: criar" in saida
    assert "preço padrao_trimestral: criar" in saida


def test_dry_run_com_valor_novo_so_le(stripe_falso, tmp_path, capsys):
    _rodar()
    stripe_falso.chamadas.clear()

    assert (
        _rodar("--dry-run", "--arquivo", str(_arquivo(tmp_path, {("essencial", "mensal"): 1}))) == 0
    )

    assert _escritas(stripe_falso) == []
    assert "preço essencial_mensal: trocar o valor" in capsys.readouterr().out


def test_produto_renomeado_ou_arquivado_e_atualizado(stripe_falso):
    _rodar()
    stripe_falso.produtos["ledgr_padrao"]["active"] = False
    stripe_falso.chamadas.clear()

    _rodar()

    assert [nome for nome in _escritas(stripe_falso)] == ["Product.modify"]
    assert stripe_falso.produtos["ledgr_padrao"]["active"] is True


def test_sem_chave_nao_faz_nada(stripe_falso, monkeypatch, capsys):
    monkeypatch.setattr(settings, "stripe_secret_key", "")

    assert _rodar() == 1

    assert stripe_falso.chamadas == []
    assert "STRIPE_SECRET_KEY" in capsys.readouterr().out


def test_chave_live_e_anunciada_como_live(stripe_falso, monkeypatch, capsys):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_falsa")

    _rodar("--dry-run")

    assert "Modo da chave: live" in capsys.readouterr().out
