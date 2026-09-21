"""Testes de volume do núcleo puro `parear_lancamentos` (issue #19), sem banco.

O motor exato agrupa por chave (valor, data) num dict, então o custo é
linear: não há comparação de todos contra todos. Estes testes guardam isso
com bases grandes. A janela de datas (buckets vizinhos) só entra com a
tolerância de data da Sprint 4 e não existe aqui.

Rodar só estes: `pytest tests/test_matching_volume.py`.
"""

import random
import statistics
import time
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from app.services.matching import LancamentoParaMatching, parear_lancamentos
from app.services.normalizacao import _normalizar_descricao
from scripts.gerar_extratos_sinteticos import _gerar_par

TETO_ABSOLUTO_SEGUNDOS = 10.0
SEED = 19


def _para_matching(lancamentos) -> list[LancamentoParaMatching]:
    """Converte lançamentos do gerador pro tipo do motor, numerando a
    ocorrência como a normalização faz (ADR-006)."""
    vistos: Counter = Counter()
    convertidos = []
    for item in lancamentos:
        chave = (item.valor, item.data, _normalizar_descricao(item.descricao))
        vistos[chave] += 1
        convertidos.append(
            LancamentoParaMatching(
                id=uuid.uuid4(),
                valor=item.valor,
                data=item.data,
                descricao=item.descricao,
                ocorrencia=vistos[chave],
            )
        )
    return convertidos


def _par_em_memoria(quantidade: int, sobreposicao: int):
    banco, sistema = _gerar_par(quantidade, sobreposicao, random.Random(SEED))
    return _para_matching(banco), _para_matching(sistema)


def _contar_status(resultados) -> Counter:
    return Counter(resultado.status for resultado in resultados)


def _mediana_de_tempos(banco, sistema, execucoes: int = 3) -> float:
    parear_lancamentos(banco, sistema)  # aquecimento, fora da medição
    tempos = []
    for _ in range(execucoes):
        inicio = time.perf_counter()
        parear_lancamentos(banco, sistema)
        tempos.append(time.perf_counter() - inicio)
    return statistics.median(tempos)


@pytest.mark.parametrize("quantidade", [10_000, 20_000, 40_000])
def test_escala_corretude_com_75_por_cento_de_sobreposicao(quantidade):
    sobreposicao = quantidade * 3 // 4
    banco, sistema = _par_em_memoria(quantidade, sobreposicao)

    contagem = _contar_status(parear_lancamentos(banco, sistema))

    assert contagem["match_exato"] == sobreposicao
    assert contagem["sem_correspondencia"] == 2 * (quantidade - sobreposicao)
    assert contagem["duplicado"] == 0


def test_escala_tempo_cresce_de_forma_aproximadamente_linear():
    pequeno = _par_em_memoria(10_000, 7_500)
    grande = _par_em_memoria(40_000, 30_000)

    tempo_10k = _mediana_de_tempos(*pequeno)
    tempo_40k = _mediana_de_tempos(*grande)
    razao = tempo_40k / tempo_10k

    # 40k é 4x o tamanho de 10k: linear daria razão perto de 4 e comportamento
    # quadrático daria perto de 16. O limite é 10 e não 4 pra ter folga contra
    # ruído de CI (máquina compartilhada, coleta de lixo, ordenação n log n):
    # ainda fica bem abaixo do quadrático, então continua detectando a regressão.
    assert (
        razao < 10
    ), f"tempo(40k)/tempo(10k) = {razao:.2f} (10k={tempo_10k:.3f}s, 40k={tempo_40k:.3f}s)"


def test_grupo_gigante_com_a_mesma_chave_gera_20k_matches_dentro_do_teto():
    quantidade = 20_000
    dia, valor = date(2026, 9, 5), Decimal("150.00")
    rng = random.Random(SEED)

    def _lado():
        itens = [
            LancamentoParaMatching(
                id=uuid.uuid4(),
                valor=valor,
                data=dia,
                descricao=f"Pagamento {indice % 5000} fornecedor {indice}",
                ocorrencia=1,
            )
            for indice in range(quantidade)
        ]
        rng.shuffle(itens)
        return itens

    banco, sistema = _lado(), _lado()

    inicio = time.perf_counter()
    resultados = parear_lancamentos(banco, sistema)
    duracao = time.perf_counter() - inicio

    assert _contar_status(resultados) == Counter({"match_exato": quantidade})
    assert duracao < TETO_ABSOLUTO_SEGUNDOS, f"grupo gigante levou {duracao:.2f}s"


def test_sem_sobreposicao_gera_40k_sem_correspondencia_dentro_do_teto():
    quantidade = 20_000
    banco, sistema = _par_em_memoria(quantidade, 0)

    inicio = time.perf_counter()
    resultados = parear_lancamentos(banco, sistema)
    duracao = time.perf_counter() - inicio

    assert _contar_status(resultados) == Counter({"sem_correspondencia": 2 * quantidade})
    assert duracao < TETO_ABSOLUTO_SEGUNDOS, f"sem sobreposição levou {duracao:.2f}s"
