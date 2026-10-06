"""Testes do spike de PDF (issue #84): parsing de valor brasileiro, pareamento
e métricas, e a avaliação de ponta a ponta com os PDFs sintéticos.

Não usam banco. Pulam (sem falhar) quando as dependências do spike não estão
instaladas, como na CI: ver scripts/spike_pdf/requirements.txt.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

from scripts.spike_pdf.avaliar import avaliar
from scripts.spike_pdf.gerar_sinteticos import DESTINO_PADRAO, gerar
from scripts.spike_pdf.modelo import Lancamento
from scripts.spike_pdf.pareamento import metricas, parear
from scripts.spike_pdf.valores import parse_data_br, parse_valor_br

# Valores e datas


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1.234,56", "1234.56"),
        ("-1.234,56", "-1234.56"),
        ("1.234,56 D", "-1234.56"),
        ("1.234,56 C", "1234.56"),
        ("(1.234,56)", "-1234.56"),
        ("1234,56", "1234.56"),
        ("0,50", "0.50"),
        ("12,00-", "-12.00"),
        ("R$ 10,00", "10.00"),
    ],
)
def test_parse_valor_br(texto, esperado):
    assert parse_valor_br(texto) == Decimal(esperado)


@pytest.mark.parametrize("texto", ["1.234", "abc", "01/02/2026", "(1,00", "1.23,45", ""])
def test_parse_valor_br_recusa_o_que_nao_e_valor(texto):
    assert parse_valor_br(texto) is None


def test_parse_data_br():
    assert parse_data_br("05/09/2026") == date(2026, 9, 5)
    assert parse_data_br("05/09/26") == date(2026, 9, 5)
    assert parse_data_br("31/02/2026") is None
    assert parse_data_br("2026-09-05") is None


# Pareamento e métricas

DIA = date(2026, 9, 5)


def _l(descricao, valor, dia=DIA):
    return Lancamento(dia, descricao, Decimal(valor))


def _medir(gabarito, extraidos):
    resultado = parear(gabarito, extraidos)
    return resultado, metricas(gabarito, extraidos, resultado)


def test_linha_certa():
    _, m = _medir([_l("Pagamento fornecedor", "-10.00")], [_l("Pagamento fornecedor", "-10.00")])

    assert (m.corretas, m.recall, m.precisao, m.erros_de_valor) == (1, 1.0, 1.0, 0)


def test_linha_faltando_e_sobrando():
    gabarito = [_l("Aluguel", "-500.00"), _l("Venda", "100.00")]
    extraidos = [_l("Aluguel", "-500.00"), _l("Tarifa", "-5.00")]

    resultado, m = _medir(gabarito, extraidos)

    assert (m.corretas, m.faltando, m.sobrando) == (1, 1, 1)
    assert resultado.faltando[0].descricao == "Venda"
    assert resultado.sobrando[0].descricao == "Tarifa"
    assert m.recall == 0.5 and m.precisao == 0.5


def test_valor_errado_e_destacado_e_nao_conta_como_faltando():
    resultado, m = _medir([_l("Aluguel", "-500.00")], [_l("Aluguel", "-50.00")])

    assert (m.corretas, m.valor_errado, m.sinal_invertido, m.faltando, m.sobrando) == (
        0,
        1,
        0,
        0,
        0,
    )
    assert m.erros_de_valor == 1
    assert resultado.valor_errado[0][1].valor == Decimal("-50.00")


def test_sinal_invertido():
    _, m = _medir([_l("Aluguel", "-500.00")], [_l("Aluguel", "500.00")])

    assert (m.sinal_invertido, m.valor_errado, m.corretas) == (1, 0, 0)


def test_descricao_levemente_diferente_casa():
    _, m = _medir(
        [_l("Pagamento de energia elétrica", "-200.00")],
        [_l("PAGAMENTO DE ENERGIA ELETRICA.", "-200.00")],
    )

    assert m.corretas == 1


def test_descricao_muito_diferente_nao_casa_e_limiar_ajustavel():
    gabarito = [_l("Pagamento de energia elétrica", "-200.00")]
    extraidos = [_l("Pagamento", "-200.00")]  # descrição cortada na quebra de linha

    assert _medir(gabarito, extraidos)[1].corretas == 0
    assert metricas(gabarito, extraidos, parear(gabarito, extraidos, limiar=0.4)).corretas == 1


def test_data_diferente_nao_casa():
    _, m = _medir([_l("Aluguel", "-500.00")], [_l("Aluguel", "-500.00", date(2026, 9, 6))])

    assert (m.corretas, m.faltando, m.sobrando) == (0, 1, 1)


def test_duas_linhas_identicas_casam_uma_com_cada():
    gabarito = [_l("Tarifa", "-5.00"), _l("Tarifa", "-5.00")]

    assert _medir(gabarito, [_l("Tarifa", "-5.00")])[1].faltando == 1
    assert _medir(gabarito, gabarito * 2)[1].sobrando == 2
    assert _medir(gabarito, list(gabarito))[1].corretas == 2


# Ponta a ponta com os sintéticos


@pytest.fixture(scope="module")
def avaliacao(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("sinteticos")
    gerar(pasta)
    saida = pasta / "saida"
    resultados = avaliar(pasta, ["tabela", "texto"], saida)
    return pasta, saida, {(r.amostra, r.estrategia): r for r in resultados}


def test_sinteticos_versionados_sao_os_que_o_gerador_produz(avaliacao):
    pasta, _, _ = avaliacao
    for arquivo in sorted(DESTINO_PADRAO.iterdir()):
        assert (pasta / arquivo.name).read_bytes() == arquivo.read_bytes(), arquivo.name


def test_escaneado_e_reconhecido_como_fora_do_escopo(avaliacao):
    _, _, resultados = avaliacao
    for estrategia in ("tabela", "texto"):
        resultado = resultados[("escaneado.pdf", estrategia)]
        assert resultado.fora_do_escopo == "PDF sem texto (escaneado), fora do escopo"
        assert resultado.metricas is None


@pytest.mark.parametrize("amostra", ["layout_a.pdf", "layout_b.pdf", "layout_c.pdf"])
def test_estrategia_texto_nos_sinteticos(avaliacao, amostra):
    _, _, resultados = avaliacao
    resultado = resultados[(amostra, "texto")]

    assert resultado.metricas.recall == 1.0
    assert resultado.metricas.erros_de_valor == 0
    assert resultado.soma_extraida == resultado.soma_gabarito
    assert resultado.configuracao is None


def test_estrategia_tabela_nos_sinteticos(avaliacao):
    _, _, resultados = avaliacao

    assert resultados[("layout_a.pdf", "tabela")].metricas.recall == 1.0
    assert resultados[("layout_b.pdf", "tabela")].metricas.recall == 1.0
    # Monoespaçado e sem grade: o pdfplumber funde o cabeçalho e corta as
    # datas, e a estratégia não extrai nada. É resultado, não defeito.
    assert resultados[("layout_c.pdf", "tabela")].total_extraido == 0
    assert all(r.metricas is None or r.metricas.erros_de_valor == 0 for r in resultados.values())


def test_relatorios_saem_completos(avaliacao):
    _, saida, _ = avaliacao
    relatorio = (saida / "relatorio.md").read_text(encoding="utf-8")
    dados = json.loads((saida / "relatorio.json").read_text(encoding="utf-8"))

    assert "## Quadro geral" in relatorio
    assert "PDF sem texto (escaneado), fora do escopo" in relatorio
    assert len(dados) == 8
    assert {"recall", "precisao", "valor_errado", "sinal_invertido"} <= set(dados[2])
    for amostra in ("layout_a", "layout_b", "layout_c"):
        for estrategia in ("tabela", "texto"):
            assert (saida / f"{amostra}.{estrategia}.diferencas.csv").exists()
