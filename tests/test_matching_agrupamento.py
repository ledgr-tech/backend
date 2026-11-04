"""Testes do núcleo puro do agrupamento N para 1 e 1 para N (issue #131,
ADR-027): `agrupar_por_soma` e a sequência de passadas de `executar_motor`.
Sem banco: só listas em memória. O banco (linhas gravadas, contagens, CHECKs)
fica em tests/test_conciliacoes_agrupamento.py, e a busca da soma em si, contra
a força bruta, em tests/test_soma_exata.py.
"""

import logging
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

import pytest
from test_soma_exata import _forca_bruta

from app.services import matching, soma_exata
from app.services.matching import (
    SCORE_MAXIMO_AGRUPAMENTO,
    LancamentoParaMatching,
    agrupar_por_soma,
    executar_motor,
)

DIA = date(2026, 9, 5)

# Dez valores diferentes que somam R$ 150.000,00
LOTE_DE_150_MIL = [
    "5000",
    "8000",
    "11000",
    "12500",
    "14000",
    "15500",
    "17000",
    "18500",
    "21000",
    "27500",
]


def _lanc(valor, data: date = DIA, descricao: str = "Pagamento", ocorrencia: int = 1):
    return LancamentoParaMatching(
        id=uuid.uuid4(),
        valor=Decimal(valor),
        data=data,
        descricao=descricao,
        ocorrencia=ocorrencia,
    )


def _lote(valores, data: date = DIA, sinal: str = "-", prefixo: str = "Parcela"):
    return [
        _lanc(f"{sinal}{valor}.00", data, f"{prefixo} {indice}")
        for indice, valor in enumerate(valores)
    ]


def _agrupados(resultados):
    return [r for r in resultados if r.status == "match_agrupado"]


def _grupos(resultados) -> set[frozenset[uuid.UUID]]:
    """Os grupos como conjuntos de ids de lançamento, sem depender do índice."""
    por_indice: dict[int, set[uuid.UUID]] = {}
    for r in _agrupados(resultados):
        por_indice.setdefault(r.grupo_id, set()).add(
            r.lancamento_banco_id or r.lancamento_sistema_id
        )
    return {frozenset(ids) for ids in por_indice.values()}


def _sem_correspondencia(resultados):
    return [r for r in resultados if r.status == "sem_correspondencia"]


def _ids_de_todos(resultados) -> list[uuid.UUID]:
    return [r.lancamento_banco_id or r.lancamento_sistema_id for r in resultados]


def _verificar_cobertura(banco, sistema, resultados):
    """Devolve resultado pra todos os lançamentos, cada um uma vez só."""
    assert sorted(_ids_de_todos(resultados)) == sorted(i.id for i in [*banco, *sistema])


# 1. lote clássico


def test_lote_classico_um_do_banco_contra_dez_do_sistema():
    banco = [_lanc("-150000.00", descricao="Pagamento em lote")]
    sistema = _lote(LOTE_DE_150_MIL)

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(resultados) == 11
    assert all(r.status == "match_agrupado" for r in resultados)
    assert {r.grupo_id for r in resultados} == {0}
    assert {r.regra_aplicada for r in resultados} == {"agrupamento"}
    # uma linha por lançamento, só a ponta dele
    assert sum(r.lancamento_banco_id is not None for r in resultados) == 1
    assert sum(r.lancamento_sistema_id is not None for r in resultados) == 10
    assert all(
        (r.lancamento_banco_id is None) != (r.lancamento_sistema_id is None) for r in resultados
    )
    _verificar_cobertura(banco, sistema, resultados)


def test_score_e_o_da_formula_e_nunca_passa_de_0_900():
    banco = [_lanc("-100.00")]
    # tolerância 0, tudo no mesmo dia: 1 - 0/1 = 1, limitado a 0,900
    mesmo_dia = agrupar_por_soma(banco, _lote(["40", "60"]), 0)
    assert (
        {r.score_confianca for r in mesmo_dia} == {SCORE_MAXIMO_AGRUPAMENTO} == {Decimal("0.900")}
    )

    # tolerância 2 e maior desvio de 2 dias: 1 - 2/3
    sistema = [_lanc("-40.00", DIA - timedelta(days=2)), _lanc("-60.00", DIA)]
    espalhado = agrupar_por_soma(banco, sistema, 2)
    assert {r.score_confianca for r in espalhado} == {Decimal("0.333")}

    # tolerância 5 e desvio de 1: 1 - 1/6 = 0,833
    perto = agrupar_por_soma(banco, [_lanc("-40.00", DIA + timedelta(days=1)), _lanc("-60.00")], 5)
    assert {r.score_confianca for r in perto} == {Decimal("0.833")}


# 2. valores repetidos e lotes de qualquer tamanho


def test_lote_de_dez_valores_iguais():
    banco = [_lanc("-150000.00")]
    sistema = [_lanc("-15000.00", descricao=f"Parcela {i}") for i in range(10)]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == 11
    assert len(_grupos(resultados)) == 1


@pytest.mark.parametrize("quantidade", [2, 10, 50, 200])
def test_sem_teto_de_tamanho_com_valores_iguais(quantidade):
    banco = [_lanc(f"-{quantidade * 1000}.00")]
    sistema = [_lanc("-1000.00", descricao=f"Parcela {i}") for i in range(quantidade)]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == quantidade + 1
    assert len(_grupos(resultados)) == 1


@pytest.mark.parametrize("quantidade", [2, 10, 50, 200])
def test_sem_teto_de_tamanho_com_valores_todos_diferentes(quantidade):
    valores = [str(500 + 7 * indice) for indice in range(quantidade)]
    banco = [_lanc(f"-{sum(int(v) for v in valores)}.00")]
    sistema = _lote(valores)

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == quantidade + 1
    assert len(_grupos(resultados)) == 1


@pytest.mark.parametrize("quantidade", [10, 50, 200])
def test_lote_no_sentido_inverso_um_do_sistema_contra_n_do_banco(quantidade):
    banco = [_lanc("-1000.00", descricao=f"Liquidação {i}") for i in range(quantidade)]
    sistema = [_lanc(f"-{quantidade * 1000}.00")]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == quantidade + 1
    assert sum(r.lancamento_sistema_id is not None for r in resultados) == 1


# 3. janela com mais candidatos que o lote


def test_janela_com_mais_candidatos_que_o_lote_agrupa_so_os_que_fecham():
    extras = ["777.77", "1313.13"]
    banco = [_lanc("-150000.00")]
    sistema = _lote(LOTE_DE_150_MIL) + [_lanc(f"-{valor}") for valor in extras]
    # premissa: só um conjunto de valores fecha os 150.000,00 entre os 12
    valores = [int(abs(i.valor) * 100) for i in sistema]
    assert len(_forca_bruta(15_000_000, valores)) == 1

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == 11
    sobraram = _sem_correspondencia(resultados)
    assert len(sobraram) == 2
    assert {r.lancamento_sistema_id for r in sobraram} == {sistema[10].id, sistema[11].id}
    _verificar_cobertura(banco, sistema, resultados)


# 4. ambiguidade real


def test_ambiguidade_real_nao_agrupa():
    banco = [_lanc("-10.00")]
    sistema = [_lanc("-1.00"), _lanc("-9.00"), _lanc("-4.00"), _lanc("-6.00")]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert _agrupados(resultados) == []
    assert len(_sem_correspondencia(resultados)) == 5
    assert all(r.grupo_id is None for r in resultados)
    _verificar_cobertura(banco, sistema, resultados)


# 5. equivalência: valores iguais são intercambiáveis


def _cenario_de_equivalencia():
    banco = [_lanc("-100.00", DIA, "PAGTO FORNECEDOR ALFA LOTE")]
    sistema = [
        _lanc("-40.00", DIA, "Parcela 1"),
        _lanc("-30.00", DIA - timedelta(days=2), "Parcela 2"),
        _lanc("-30.00", DIA, "Parcela 3"),
        _lanc("-30.00", DIA + timedelta(days=1), "Parcela 4"),
        _lanc("-7.31"),
        _lanc("-13.57"),
        _lanc("-2.11"),
        _lanc("-3.03"),
        _lanc("-1.07"),
        _lanc("-11.11"),
        _lanc("-0.97"),
    ]
    valores = [int(abs(i.valor) * 100) for i in sistema]
    assert len(_forca_bruta(10_000, valores)) == 1  # premissa: um multiconjunto só
    return banco, sistema


def test_equivalencia_prefere_o_menor_desvio_de_data():
    banco, sistema = _cenario_de_equivalencia()

    resultados = agrupar_por_soma(banco, sistema, 2)

    # entram o de 40 e os dois de 30 mais perto de DIA (desvios 0 e 1), não o de DIA - 2
    assert _grupos(resultados) == {
        frozenset({banco[0].id, sistema[0].id, sistema[2].id, sistema[3].id})
    }
    assert sistema[1].id in {r.lancamento_sistema_id for r in _sem_correspondencia(resultados)}


def test_equivalencia_desempata_por_descricao_parecida():
    banco = [_lanc("-100.00", DIA, "PAGTO FORNECEDOR ALFA LOTE")]
    sistema = [
        _lanc("-40.00", DIA, "Parcela"),
        _lanc("-30.00", DIA, "Parcela"),
        _lanc("-30.00", DIA - timedelta(days=1), "COMPRA MATERIAL DE LIMPEZA"),
        _lanc("-30.00", DIA + timedelta(days=1), "PAGTO FORNECEDOR ALFA"),
    ]

    resultados = agrupar_por_soma(banco, sistema, 2)

    # os dois de 30 que sobram disputam por data: o de DIA entra, e o desempate dos
    # outros dois (ambos a 1 dia) é a descrição mais parecida com a do banco
    assert _grupos(resultados) == {
        frozenset({banco[0].id, sistema[0].id, sistema[1].id, sistema[3].id})
    }


def test_mesma_entrada_em_ordem_permutada_gera_o_mesmo_resultado():
    banco, sistema = _cenario_de_equivalencia()
    esperado = _grupos(agrupar_por_soma(banco, sistema, 2))
    rng = random.Random(5)
    for _ in range(25):
        embaralhado = sistema[:]
        rng.shuffle(embaralhado)
        assert _grupos(agrupar_por_soma(banco, embaralhado, 2)) == esperado


# 6. um do sistema contra vários do banco


def test_um_para_n_sistema_contra_tres_do_banco():
    banco = [_lanc("-1000.00"), _lanc("-2000.00"), _lanc("-3000.00")]
    sistema = [_lanc("-6000.00")]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == 4
    assert sum(r.lancamento_banco_id is not None for r in resultados) == 3
    assert sum(r.lancamento_sistema_id is not None for r in resultados) == 1


# 7. os dois sentidos no mesmo par e na mesma data


def _cenario_dos_dois_sentidos():
    banco = [_lanc("-150000.00", descricao="Lote")] + [
        _lanc(f"-{valor}.00", descricao=f"Linha {valor}") for valor in (1000, 2000, 3000)
    ]
    sistema = _lote(LOTE_DE_150_MIL) + [_lanc("-6000.00", descricao="Fornecedor")]
    return banco, sistema


def test_dois_grupos_de_sentidos_opostos_sem_um_consumir_o_outro():
    banco, sistema = _cenario_dos_dois_sentidos()

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert _sem_correspondencia(resultados) == []
    assert _grupos(resultados) == {
        frozenset({banco[0].id, *(i.id for i in sistema[:10])}),
        frozenset({sistema[10].id, *(i.id for i in banco[1:])}),
    }
    assert len({r.grupo_id for r in resultados}) == 2
    _verificar_cobertura(banco, sistema, resultados)


def test_dois_sentidos_em_qualquer_ordem_de_entrada():
    banco, sistema = _cenario_dos_dois_sentidos()
    esperado = _grupos(agrupar_por_soma(banco, sistema, 0))
    rng = random.Random(7)
    for _ in range(15):
        b, s = banco[:], sistema[:]
        rng.shuffle(b)
        rng.shuffle(s)
        assert _grupos(agrupar_por_soma(b, s, 0)) == esperado


# 8. dois lotes no mesmo par: ponto fixo e impasse


def test_um_grupo_desfaz_a_ambiguidade_do_outro():
    # A = 20, B = 11 (valores em milhares). Com os cinco do sistema, A é ambíguo
    # ({5,15}, {8,12}, {3,5,12}); B, só com os menores que 11, tem {3,8} e mais nada.
    banco = [_lanc("-20000.00", descricao="Lote A"), _lanc("-11000.00", descricao="Lote B")]
    sistema = [_lanc(f"-{valor}000.00", descricao=f"Linha {valor}") for valor in (3, 8, 12, 5, 15)]

    resultados = agrupar_por_soma(banco, sistema, 0)

    # B fecha {3,8}; sem esses, A deixa de ser ambíguo e fecha {5,15}
    assert _grupos(resultados) == {
        frozenset({banco[1].id, sistema[0].id, sistema[1].id}),
        frozenset({banco[0].id, sistema[3].id, sistema[4].id}),
    }
    assert [r.lancamento_sistema_id for r in _sem_correspondencia(resultados)] == [sistema[2].id]


def test_quando_nada_resolve_a_ambiguidade_nenhum_agrupa():
    # A = 20 ({5,6,9}, {3,8,9}) e B = 11 ({3,8}, {5,6}) se travam um ao outro
    banco = [_lanc("-20000.00"), _lanc("-11000.00")]
    sistema = [_lanc(f"-{valor}000.00") for valor in (3, 8, 5, 6, 9)]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert _agrupados(resultados) == []
    assert len(_sem_correspondencia(resultados)) == 7


# 9. janela de data


def test_tolerancia_zero_nao_agrupa_lancamentos_de_dias_diferentes():
    banco = [_lanc("-100.00")]
    sistema = [_lanc("-40.00"), _lanc("-60.00", DIA + timedelta(days=1))]

    assert _agrupados(agrupar_por_soma(banco, sistema, 0)) == []


def test_tolerancia_dois_agrupa_lancamentos_espalhados_em_ate_dois_dias():
    banco = [_lanc("-100.00")]
    sistema = [
        _lanc("-40.00", DIA - timedelta(days=2)),
        _lanc("-30.00", DIA + timedelta(days=2)),  # a 4 dias do primeiro: até 2x a tolerância
        _lanc("-30.00"),
        _lanc("-5.00", DIA + timedelta(days=3)),  # a 3 dias do banco: fora da janela
    ]

    resultados = agrupar_por_soma(banco, sistema, 2)

    assert _grupos(resultados) == {frozenset({banco[0].id, *(i.id for i in sistema[:3])})}
    assert [r.lancamento_sistema_id for r in _sem_correspondencia(resultados)] == [sistema[3].id]


def test_lancamento_a_tres_dias_so_entra_com_tolerancia_tres():
    banco = [_lanc("-100.00")]
    sistema = [_lanc("-40.00"), _lanc("-60.00", DIA + timedelta(days=3))]

    assert _agrupados(agrupar_por_soma(banco, sistema, 2)) == []
    com_tres = agrupar_por_soma(banco, sistema, 3)
    assert len(_agrupados(com_tres)) == 3
    assert {r.score_confianca for r in com_tres} == {Decimal("0.250")}  # 1 - 3/4


def test_lancamento_fora_da_janela_nao_atrapalha_se_a_soma_fecha_sem_ele():
    banco = [_lanc("-100.00")]
    sistema = [_lanc("-40.00"), _lanc("-60.00"), _lanc("-7.00", DIA + timedelta(days=9))]

    resultados = agrupar_por_soma(banco, sistema, 2)

    assert _grupos(resultados) == {frozenset({banco[0].id, sistema[0].id, sistema[1].id})}


# 10. janelas sobrepostas


@pytest.mark.parametrize("tolerancia", [2, 3])
def test_janelas_sobrepostas_resultado_deterministico_e_sem_lancamento_em_dois_grupos(tolerancia):
    # A = 100 em DIA e B = 60 em DIA + 1 disputam os mesmos candidatos. Maior primeiro:
    # A fecha {35,25,40}; B, que sozinho teria {35,25} ou {40,20}, fica sem o que usar.
    banco = [_lanc("-100.00", DIA, "A"), _lanc("-60.00", DIA + timedelta(days=1), "B")]
    sistema = [
        _lanc("-35.00", DIA),
        _lanc("-25.00", DIA + timedelta(days=1)),
        _lanc("-40.00", DIA + timedelta(days=2)),
        _lanc("-20.00", DIA + timedelta(days=1)),
    ]

    resultados = agrupar_por_soma(banco, sistema, tolerancia)

    assert _grupos(resultados) == {frozenset({banco[0].id, *(i.id for i in sistema[:3])})}
    assert sorted(_ids_de_todos(resultados)) == sorted(i.id for i in [*banco, *sistema])
    assert len(_ids_de_todos(_agrupados(resultados))) == len(
        set(_ids_de_todos(_agrupados(resultados)))
    )

    rng = random.Random(tolerancia)
    for _ in range(15):
        b, s = banco[:], sistema[:]
        rng.shuffle(b)
        rng.shuffle(s)
        assert _grupos(agrupar_por_soma(b, s, tolerancia)) == _grupos(resultados)


# 11. sinais mistos e diferença de um centavo


def test_sinais_mistos_que_somariam_o_alvo_nao_agrupam():
    # +160 e -60 somam 100 em álgebra, mas crédito não agrupa com débito
    assert (
        _agrupados(agrupar_por_soma([_lanc("100.00")], [_lanc("160.00"), _lanc("-60.00")], 0)) == []
    )
    # e -130 com +30 contra -100
    assert (
        _agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-130.00"), _lanc("30.00")], 0))
        == []
    )
    # mesmo sinal fecha: créditos com crédito
    credito = agrupar_por_soma([_lanc("100.00")], [_lanc("40.00"), _lanc("60.00")], 0)
    assert len(_agrupados(credito)) == 3


def test_soma_com_um_centavo_de_diferenca_nao_agrupa():
    assert (
        _agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-60.00"), _lanc("-39.99")], 0))
        == []
    )
    assert (
        _agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-60.00"), _lanc("-40.01")], 0))
        == []
    )
    assert (
        len(_agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-60.01"), _lanc("-39.99")], 0)))
        == 3
    )


# 12. prioridade do 1 para 1, duplicado, zero e tarifa


def test_um_para_um_tem_prioridade_e_o_grupo_nao_fecha():
    # O banco tem o lote de 150 mil e um pagamento individual de 11.000,00 no mesmo dia; o
    # sistema tem o lote em 10 linhas e nenhuma outra de 11.000,00. O 1 para 1 consome o
    # lançamento do lote e o grupo não fecha.
    banco = [_lanc("-150000.00", descricao="Lote"), _lanc("-11000.00", descricao="Individual")]
    sistema = _lote(LOTE_DE_150_MIL)

    resultados = executar_motor(banco, sistema, 0, agrupar=True)

    assert _agrupados(resultados) == []
    pares = [r for r in resultados if r.status == "match_exato"]
    assert len(pares) == 1 and pares[0].lancamento_sistema_id == sistema[2].id
    # o lote continua como divergência: 1 do banco e 9 do sistema
    assert sum(r.status != "match_exato" for r in resultados) == 10


def test_sem_o_pagamento_individual_o_mesmo_lote_agrupa_pela_sequencia_do_motor():
    banco = [_lanc("-150000.00", descricao="Lote")]
    sistema = _lote(LOTE_DE_150_MIL)

    resultados = executar_motor(banco, sistema, 0, agrupar=True)

    assert len(_agrupados(resultados)) == 11 and len(resultados) == 11


def test_duplicado_nunca_entra_no_agrupamento():
    # banco -40 aparece duas vezes e o sistema tem um só: um par exato, e o outro -40 do
    # banco é duplicado. Se o duplicado valesse, -60 + -40 fecharia o -100 do sistema.
    banco = [_lanc("-40.00"), _lanc("-40.00"), _lanc("-60.00")]
    sistema = [_lanc("-40.00"), _lanc("-100.00")]

    resultados = executar_motor(banco, sistema, 0, agrupar=True)

    assert _agrupados(resultados) == []
    assert sum(r.status == "duplicado" for r in resultados) == 1


def test_lancamento_de_valor_zero_e_ignorado():
    banco = [_lanc("-100.00")]
    sistema = [_lanc("-60.00"), _lanc("-40.00"), _lanc("0.00"), _lanc("-0.00")]

    resultados = agrupar_por_soma(banco, sistema, 0)

    assert len(_agrupados(resultados)) == 3
    assert {r.lancamento_sistema_id for r in _sem_correspondencia(resultados)} == {
        sistema[2].id,
        sistema[3].id,
    }


def test_lancamento_do_banco_com_termo_de_tarifa_fica_fora_do_agrupamento():
    # como lançamento único
    tarifa = [_lanc("-100.00", descricao="TARIFA PACOTE SERVICOS")]
    assert _agrupados(agrupar_por_soma(tarifa, [_lanc("-60.00"), _lanc("-40.00")], 0)) == []
    # como membro de um grupo do sistema
    banco = [_lanc("-60.00", descricao="Tarifa de manutenção"), _lanc("-40.00")]
    assert _agrupados(agrupar_por_soma(banco, [_lanc("-100.00")], 0)) == []
    # e a tarifa segue pra classificação na sequência do motor
    classificado = executar_motor(tarifa, [_lanc("-60.00"), _lanc("-40.00")], 0, agrupar=True)
    assert "tarifa_bancaria" in {r.status for r in classificado}


# 13. janela patológica


def test_janela_patologica_termina_no_teto_nao_agrupa_e_loga_so_ids_e_contagens(caplog):
    banco = [_lanc("-300000.00", descricao="DESCRICAO SECRETA DO BANCO")]
    sistema = [
        _lanc(f"-{10000 + indice * 0.17:.2f}", descricao=f"DESCRICAO SECRETA {indice}")
        for indice in range(300)
    ]

    with caplog.at_level(logging.WARNING, logger="app.services.matching"):
        resultados = agrupar_por_soma(banco, sistema, 0)

    assert _agrupados(resultados) == []
    assert len(_sem_correspondencia(resultados)) == 301
    _verificar_cobertura(banco, sistema, resultados)
    mensagens = [r.getMessage() for r in caplog.records]
    assert any(str(banco[0].id) in m and "candidatos=300" in m for m in mensagens)
    for mensagem in mensagens:
        assert "SECRETA" not in mensagem
        assert "300000.00" not in mensagem and "2026-09" not in mensagem


def test_teto_de_trabalho_da_conciliacao_inteira_nao_agrupa_o_que_precisa_de_busca_pesada(
    monkeypatch, caplog
):
    banco = [_lanc("-150000.00")]
    sistema = _lote(LOTE_DE_150_MIL) + [_lanc("-777.77"), _lanc("-1313.13"), _lanc("-91.45")]

    assert len(_agrupados(agrupar_por_soma(banco, sistema, 0))) == 11  # com o teto normal

    monkeypatch.setattr(matching, "LIMITE_TRABALHO_TOTAL", 0)
    monkeypatch.setattr(soma_exata, "LIMITE_ENUMERACAO", 0)  # força a busca pesada
    with caplog.at_level(logging.WARNING, logger="app.services.matching"):
        resultados = agrupar_por_soma(banco, sistema, 0)
    assert _agrupados(resultados) == []
    assert len(_sem_correspondencia(resultados)) == 14
    assert any(str(banco[0].id) in r.getMessage() for r in caplog.records)


# 14. propriedade: bate com a força bruta


def _desvio_total(indices, candidatos, unico):
    return sum(abs((candidatos[i].data - unico.data).days) for i in indices)


def test_resultado_bate_com_a_forca_bruta_em_instancias_aleatorias():
    rng = random.Random(131)
    contagens = {"unico": 0, "ambiguo_ou_nenhum": 0}
    for _ in range(1500):
        tolerancia = rng.choice([0, 0, 1, 2, 3])
        sinal = rng.choice(["", "-"])
        quantidade = rng.randint(2, 9)
        candidatos = [
            _lanc(
                f"{sinal}{rng.randint(1, 40)}.00",
                DIA + timedelta(days=rng.randint(-3, 3)),
                rng.choice(["Parcela A", "Parcela B", "Outra"]),
                ocorrencia=indice,
            )
            for indice in range(quantidade)
        ]
        janela = [
            i
            for i, c in enumerate(candidatos)
            if abs((c.data - DIA).days) <= tolerancia and abs(c.valor) < 120
        ]
        alvo_valor = (
            sum(
                abs(candidatos[i].valor)
                for i in rng.sample(janela, min(len(janela), rng.randint(2, 4)))
            )
            if len(janela) >= 2
            else Decimal(rng.randint(2, 90))
        )
        unico = _lanc(f"{sinal}{alvo_valor}", DIA, "Lote")
        janela = [i for i in janela if abs(candidatos[i].valor) < abs(unico.valor)]

        resultados = agrupar_por_soma([unico], candidatos, tolerancia)

        valores = [int(abs(candidatos[i].valor) * 100) for i in janela]
        conjuntos = _forca_bruta(int(abs(unico.valor) * 100), valores)
        grupo = _grupos(resultados)
        if len(conjuntos) == 1:
            contagens["unico"] += 1
            (esperado,) = conjuntos
            assert len(grupo) == 1, (unico, candidatos)
            membros = [i for i, c in enumerate(candidatos) if c.id in next(iter(grupo))]
            assert tuple(sorted(int(abs(candidatos[i].valor) * 100) for i in membros)) == esperado
            assert all(i in janela for i in membros)
            melhor = min(
                _desvio_total(indices, candidatos, unico)
                for tamanho in range(2, len(janela) + 1)
                for indices in combinations(janela, tamanho)
                if tuple(sorted(int(abs(candidatos[i].valor) * 100) for i in indices)) == esperado
            )
            assert _desvio_total(membros, candidatos, unico) == melhor
        else:
            contagens["ambiguo_ou_nenhum"] += 1
            assert grupo == set(), (unico, candidatos)
        _verificar_cobertura([unico], candidatos, resultados)
    assert contagens["unico"] > 100 and contagens["ambiguo_ou_nenhum"] > 100


def test_invariantes_em_instancias_com_os_dois_lados_e_ordem_embaralhada():
    rng = random.Random(1310)
    com_grupo = 0
    for _ in range(600):
        tolerancia = rng.choice([0, 1, 2])
        sinais = ["", "-", "-", "-"]
        banco = [
            _lanc(
                f"{rng.choice(sinais)}{rng.randint(1, 14)}.00",
                DIA + timedelta(days=rng.randint(-2, 2)),
                rng.choice(["A", "B"]),
                ocorrencia=indice,
            )
            for indice in range(rng.randint(1, 7))
        ]
        sistema = [
            _lanc(
                f"{rng.choice(sinais)}{rng.randint(1, 14)}.00",
                DIA + timedelta(days=rng.randint(-2, 2)),
                rng.choice(["A", "B"]),
                ocorrencia=indice,
            )
            for indice in range(rng.randint(1, 7))
        ]
        resultados = agrupar_por_soma(banco, sistema, tolerancia)
        _verificar_cobertura(banco, sistema, resultados)
        por_id = {i.id: i for i in [*banco, *sistema]}
        grupos: dict[int, list] = {}
        for r in _agrupados(resultados):
            grupos.setdefault(r.grupo_id, []).append(r)
        for linhas in grupos.values():
            com_grupo += 1
            do_banco = [por_id[r.lancamento_banco_id] for r in linhas if r.lancamento_banco_id]
            do_sistema = [
                por_id[r.lancamento_sistema_id] for r in linhas if r.lancamento_sistema_id
            ]
            unicos, membros = (
                (do_banco, do_sistema) if len(do_banco) == 1 else (do_sistema, do_banco)
            )
            assert len(unicos) == 1 and len(membros) >= 2
            assert sum(m.valor for m in membros) == unicos[0].valor
            assert all(
                (m.valor > 0) == (unicos[0].valor > 0)
                and abs((m.data - unicos[0].data).days) <= tolerancia
                for m in membros
            )
        embaralhado_b, embaralhado_s = banco[:], sistema[:]
        rng.shuffle(embaralhado_b)
        rng.shuffle(embaralhado_s)
        assert _grupos(agrupar_por_soma(embaralhado_b, embaralhado_s, tolerancia)) == _grupos(
            resultados
        )
    assert com_grupo > 100


# contrato geral


def test_sem_um_dos_lados_ou_sem_candidatos_devolve_tudo_sem_correspondencia():
    assert agrupar_por_soma([], [], 0) == []
    so_banco = [_lanc("-10.00"), _lanc("-20.00")]
    resultados = agrupar_por_soma(so_banco, [], 0)
    assert [r.status for r in resultados] == ["sem_correspondencia"] * 2
    assert all(r.grupo_id is None and r.regra_aplicada is None for r in resultados)


def test_um_unico_candidato_nao_forma_grupo():
    # grupo de um membro só seria 1 para 1, que não é desta etapa
    assert _agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-60.00")], 0)) == []
    assert _agrupados(agrupar_por_soma([_lanc("-100.00")], [_lanc("-100.00")], 0)) == []


def test_etapa_roda_com_tolerancia_zero_e_a_sequencia_do_motor_so_agrupa_com_a_flag():
    banco = [_lanc("-100.00")]
    sistema = [_lanc("-60.00"), _lanc("-40.00")]

    ligado = executar_motor(banco, sistema, 0, agrupar=True)
    desligado = executar_motor(banco, sistema, 0, agrupar=False)

    assert {r.status for r in ligado} == {"match_agrupado"}
    assert "match_agrupado" not in {r.status for r in desligado}
    assert {r.status for r in desligado} == {"divergente_valor"}  # como o motor era antes
