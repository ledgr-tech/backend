"""Testes da busca exata de soma (issue #131, ADR-027), sem banco: o resultado
de `buscar_conjunto_unico` bate com a força bruta em instâncias aleatórias
pequenas, pelos dois caminhos (enumeração e programação dinâmica), e o teto de
trabalho abandona a busca sem inventar resposta.

Sem `hypothesis` nas dependências: o gerador usa semente fixa e muitas
instâncias, então a falha é reproduzível.
"""

import random
import time
from itertools import combinations

import pytest

from app.services import soma_exata
from app.services.soma_exata import AMBIGUO, ESTOUROU, NENHUM, UNICO, buscar_conjunto_unico

SEMENTE = 131


def _forca_bruta(alvo: int, valores: list[int]) -> set[tuple[int, ...]]:
    """Todos os multiconjuntos de 2 ou mais valores, tirados de `valores`
    (índice a índice), cuja soma é o alvo."""
    conjuntos = set()
    for tamanho in range(2, len(valores) + 1):
        for indices in combinations(range(len(valores)), tamanho):
            escolhidos = tuple(sorted(valores[i] for i in indices))
            if sum(escolhidos) == alvo:
                conjuntos.add(escolhidos)
    return conjuntos


def _como_multiconjunto(quantidades: dict[int, int]) -> tuple[int, ...]:
    return tuple(sorted(valor for valor, q in quantidades.items() for _ in range(q)))


def _verificar(alvo: int, valores: list[int]) -> str:
    esperado = _forca_bruta(alvo, valores)
    busca = buscar_conjunto_unico(alvo, valores)
    if not esperado:
        assert busca.situacao == NENHUM, (alvo, valores)
    elif len(esperado) > 1:
        assert busca.situacao == AMBIGUO, (alvo, valores)
    else:
        assert busca.situacao == UNICO, (alvo, valores)
        assert _como_multiconjunto(busca.quantidades) == next(iter(esperado)), (alvo, valores)
    return busca.situacao


def _instancia(rng: random.Random) -> tuple[int, list[int]]:
    quantidade = rng.randint(2, 11)
    maximo = rng.choice([5, 12, 40, 300, 5000])
    base = rng.choice([1, 1, 1, 7, 100])  # múltiplos de um divisor comum, às vezes
    valores = [rng.randint(1, maximo) * base for _ in range(quantidade)]
    if rng.random() < 0.7:
        sorteados = rng.sample(valores, rng.randint(2, quantidade))
        alvo = sum(sorteados)
    else:
        alvo = rng.randint(2, sum(valores) + base)
    return alvo, valores


@pytest.mark.parametrize("limite_enumeracao", [soma_exata.LIMITE_ENUMERACAO, 0])
def test_resultado_bate_com_a_forca_bruta_em_muitas_instancias(monkeypatch, limite_enumeracao):
    # 0 força a programação dinâmica mesmo nas instâncias pequenas
    monkeypatch.setattr(soma_exata, "LIMITE_ENUMERACAO", limite_enumeracao)
    rng = random.Random(SEMENTE)
    situacoes = set()
    for _ in range(4000):
        alvo, valores = _instancia(rng)
        situacoes.add(_verificar(alvo, valores))
    assert situacoes == {UNICO, NENHUM, AMBIGUO}  # o gerador exercita as três respostas


def test_ordem_dos_valores_nao_muda_a_resposta():
    rng = random.Random(SEMENTE + 1)
    for _ in range(300):
        alvo, valores = _instancia(rng)
        esperado = buscar_conjunto_unico(alvo, valores)
        embaralhados = valores[:]
        rng.shuffle(embaralhados)
        assert buscar_conjunto_unico(alvo, embaralhados) == esperado


def test_valores_iguais_sao_intercambiaveis_e_nao_geram_ambiguidade():
    # 15.000,00 x 10 contra 150.000,00 (em centavos): um conjunto só
    busca = buscar_conjunto_unico(15_000_000, [1_500_000] * 12)
    assert busca.situacao == UNICO
    assert busca.quantidades == {1_500_000: 10}


@pytest.mark.parametrize("tamanho", [2, 10, 50, 200])
def test_sem_teto_de_tamanho_do_grupo(tamanho):
    valores = [100 + indice for indice in range(tamanho)]  # todos diferentes
    busca = buscar_conjunto_unico(sum(valores), valores + [sum(valores) + 1])
    assert busca.situacao == UNICO
    assert busca.quantidades == {valor: 1 for valor in valores}


def test_sobras_pequenas_no_meio_de_muitos_candidatos_diferentes():
    # lote de 40 valores diferentes e 3 sobras que não fazem outro conjunto de mesma soma
    lote = [10_000 + 37 * indice for indice in range(40)]
    sobras = [10_001, 10_002, 10_004]
    busca = buscar_conjunto_unico(sum(lote), lote + sobras)
    assert busca.situacao == UNICO and busca.quantidades == {valor: 1 for valor in lote}


def test_ambiguidade_com_valores_diferentes_de_mesma_soma():
    assert buscar_conjunto_unico(10, [1, 9, 4, 6]).situacao == AMBIGUO
    assert buscar_conjunto_unico(10, [1, 9, 5]).situacao == UNICO


def test_um_so_candidato_ou_valor_igual_ao_alvo_nao_forma_grupo():
    assert buscar_conjunto_unico(10, [10, 3]).situacao == NENHUM
    assert buscar_conjunto_unico(10, [10]).situacao == NENHUM
    assert buscar_conjunto_unico(10, [9]).situacao == NENHUM
    assert buscar_conjunto_unico(10, [10, 4, 6]).quantidades == {4: 1, 6: 1}


def test_soma_um_centavo_diferente_nao_agrupa():
    assert buscar_conjunto_unico(1000, [500, 499]).situacao == NENHUM
    assert buscar_conjunto_unico(1000, [500, 501]).situacao == NENHUM


def test_teto_de_trabalho_abandona_a_busca_sem_resposta():
    # tipos diferentes demais para enumerar e alvo grande: muito acima de um teto pequeno
    valores = [1_000_003 + 17 * indice for indice in range(60)]
    alvo = sum(valores[:30]) + 7
    assert buscar_conjunto_unico(alvo, valores, limite_trabalho=1_000).situacao == ESTOUROU


def test_janela_patologica_estoura_o_teto_padrao_sem_travar():
    # 300 valores diferentes e alvo de R$ 300.000,00: fora do teto padrão
    valores = [1_000_003 + 17 * indice for indice in range(300)]
    inicio = time.perf_counter()
    busca = buscar_conjunto_unico(30_000_007, valores)
    assert busca.situacao == ESTOUROU
    assert time.perf_counter() - inicio < 1.0  # abandona na estimativa, antes de mexer em bits


def test_lote_de_cliente_cabe_no_teto_padrao():
    # R$ 150.000,00 contra 50 valores diferentes: o caso real, bem dentro do teto
    valores = [250_000 + 1_117 * indice for indice in range(50)]
    alvo = sum(valores[:12])
    busca = buscar_conjunto_unico(alvo, valores)
    assert busca.situacao in {UNICO, AMBIGUO}
    assert 0 < busca.trabalho < soma_exata.LIMITE_TRABALHO


def test_teto_de_trabalho_nao_depende_do_relogio():
    valores = [1_000_003 + 17 * indice for indice in range(60)]
    alvo = sum(valores[:30]) + 7
    respostas = [buscar_conjunto_unico(alvo, valores, limite_trabalho=1_000) for _ in range(3)]
    assert respostas[0] == respostas[1] == respostas[2]
