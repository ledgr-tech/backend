"""Testes de volume do núcleo puro `parear_lancamentos` (issue #19), sem banco.

O motor exato agrupa por chave (valor, data) num dict, então o custo é
linear: não há comparação de todos contra todos. Estes testes guardam isso
com bases grandes. A janela de datas (buckets vizinhos) só entra com a
tolerância de data da Sprint 4 e não existe aqui.

Têm o marcador `volume` (issue #74) e rodam no job `volume` da CI, separado
da suíte principal. Rodar só estes: `pytest -m volume tests/test_matching_volume.py`.

O teto absoluto de 10 s ficou (issue #74): aqui não há banco nem HTTP, e os
casos medidos levam bem menos que 1 s (0,18 s no grupo gigante e 0,60 s sem
sobreposição, em 07/10), então a folga é de mais de 15 vezes. O teste de
crescimento relativo segue como o detector de regressão do caso geral, e o
teto do grupo gigante é o que pega uma comparação de todos contra todos
dentro de um grupo de mesma chave.
"""

import random
import statistics
import time
import uuid
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.matching import LancamentoParaMatching, agrupar_por_soma, parear_lancamentos
from app.services.normalizacao import _normalizar_descricao
from scripts.gerar_extratos_sinteticos import _gerar_par

pytestmark = pytest.mark.volume

TETO_ABSOLUTO_SEGUNDOS = 10.0
# Teto das janelas densas do agrupamento (issue #131): 40 mil lançamentos com uns 20 por
# lado por dia, quase todos disputando o teto de trabalho da conciliação. É rede contra
# travamento, não detector de regressão.
TETO_AGRUPAMENTO_DENSO_SEGUNDOS = 30.0
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


# Agrupamento N para 1 e 1 para N (issue #131, ADR-027)


def _sem_par_espalhados(por_lado: int, datas: int, rng, faixa=(100, 500_000)):
    """`por_lado` lançamentos sem par de cada lado, de débito, espalhados em
    `datas` dias e com valores aleatórios (em centavos), a mesma densidade por
    dia qualquer que seja o tamanho."""
    inicio = date(2024, 1, 1)

    def _lado(prefixo):
        return [
            LancamentoParaMatching(
                id=uuid.uuid4(),
                valor=-Decimal(rng.randint(*faixa)) / 100,
                data=inicio + timedelta(days=rng.randrange(datas)),
                descricao=f"{prefixo} {indice}",
                ocorrencia=1,
            )
            for indice in range(por_lado)
        ]

    return _lado("banco"), _lado("sistema")


def _verificar_grupos_exatos(banco, sistema, resultados, tolerancia):
    """Cada lançamento aparece uma vez, e todo grupo soma ao centavo, com o
    mesmo sinal e dentro da janela de data."""
    por_id = {i.id: i for i in [*banco, *sistema]}
    ids = [r.lancamento_banco_id or r.lancamento_sistema_id for r in resultados]
    assert len(ids) == len(set(ids)) == len(por_id)
    grupos: dict[int, list] = {}
    for r in resultados:
        if r.status == "match_agrupado":
            grupos.setdefault(r.grupo_id, []).append(r)
    for linhas in grupos.values():
        do_banco = [por_id[r.lancamento_banco_id] for r in linhas if r.lancamento_banco_id]
        do_sistema = [por_id[r.lancamento_sistema_id] for r in linhas if r.lancamento_sistema_id]
        unicos, membros = (do_banco, do_sistema) if len(do_banco) == 1 else (do_sistema, do_banco)
        assert len(unicos) == 1 and len(membros) >= 2
        assert sum(m.valor for m in membros) == unicos[0].valor
        assert all(abs((m.data - unicos[0].data).days) <= tolerancia for m in membros)
    return len(grupos)


def _mediana_do_agrupamento(banco, sistema, tolerancia, execucoes: int = 3) -> float:
    agrupar_por_soma(banco, sistema, tolerancia)  # aquecimento, fora da medição
    tempos = []
    for _ in range(execucoes):
        inicio = time.perf_counter()
        agrupar_por_soma(banco, sistema, tolerancia)
        tempos.append(time.perf_counter() - inicio)
    return statistics.median(tempos)


@pytest.mark.parametrize(
    ("tolerancia", "datas"),
    [(0, 5_000), (2, 5_000), (0, 1_000), (2, 1_000)],
    ids=["4-por-dia", "4-por-dia-tol-2", "20-por-dia", "20-por-dia-tol-2"],
)
def test_agrupamento_com_dezenas_de_milhares_sem_par_espalhados_em_muitas_datas(tolerancia, datas):
    # 20 mil de cada lado (40 mil lançamentos), uns 4 ou 20 por lado por dia
    banco, sistema = _sem_par_espalhados(20_000, datas, random.Random(SEED))

    inicio = time.perf_counter()
    resultados = agrupar_por_soma(banco, sistema, tolerancia)
    duracao = time.perf_counter() - inicio
    print(
        f"\nTEMPO_AGRUPAMENTO tolerancia={tolerancia} datas={datas} lancamentos=40000 "
        f"segundos={duracao:.2f}"
    )

    # Com tantos valores aleatórios por dia há coincidências de soma que fecham grupo (e
    # quem tem mais de um conjunto não agrupa): o que importa aqui é que todo grupo é exato.
    _verificar_grupos_exatos(banco, sistema, resultados, tolerancia)
    teto = TETO_AGRUPAMENTO_DENSO_SEGUNDOS if datas < 5_000 else TETO_ABSOLUTO_SEGUNDOS
    assert duracao < teto, f"agrupamento levou {duracao:.2f}s"


def test_agrupamento_tempo_cresce_de_forma_aproximadamente_linear():
    rng = random.Random(SEED)
    pequeno = _sem_par_espalhados(5_000, 1_250, rng)  # a mesma densidade por dia
    grande = _sem_par_espalhados(20_000, 5_000, rng)

    tempo_5k = _mediana_do_agrupamento(*pequeno, 0)
    tempo_20k = _mediana_do_agrupamento(*grande, 0)
    razao = tempo_20k / tempo_5k
    print(f"\nTEMPO_AGRUPAMENTO_RELATIVO 5k={tempo_5k:.2f}s 20k={tempo_20k:.2f}s razao={razao:.2f}")

    # 20k é 4x o tamanho de 5k: linear dá perto de 4 e quadrático, perto de 16
    assert razao < 8, f"tempo(20k)/tempo(5k) = {razao:.2f}"


def test_lote_de_150_mil_e_achado_no_meio_de_dezenas_de_milhares_sem_par():
    # Ruído em que nenhum grupo é possível: com valores entre R$ 1.000,00 e R$ 1.999,99, a soma
    # de dois já passa de qualquer um. O lote de 150 mil é o único grupo.
    rng = random.Random(SEED)
    banco, sistema = _sem_par_espalhados(20_000, 1_000, rng, faixa=(100_000, 199_999))
    dia = date(2024, 1, 1) + timedelta(days=6_000)  # fora das datas do ruído
    lote_banco = LancamentoParaMatching(uuid.uuid4(), Decimal("-150000.00"), dia, "lote", 1)
    valores = [5000, 8000, 11000, 12500, 14000, 15500, 17000, 18500, 21000, 27500]
    lote_sistema = [
        LancamentoParaMatching(uuid.uuid4(), Decimal(f"-{v}.00"), dia, f"parcela {v}", 1)
        for v in valores
    ]

    inicio = time.perf_counter()
    resultados = agrupar_por_soma([*banco, lote_banco], [*sistema, *lote_sistema], 0)
    duracao = time.perf_counter() - inicio

    assert (
        _verificar_grupos_exatos([*banco, lote_banco], [*sistema, *lote_sistema], resultados, 0)
        == 1
    )
    agrupados = {
        r.lancamento_banco_id or r.lancamento_sistema_id
        for r in resultados
        if r.status == "match_agrupado"
    }
    assert agrupados == {lote_banco.id, *(i.id for i in lote_sistema)}
    assert duracao < TETO_ABSOLUTO_SEGUNDOS


def test_janela_patologica_em_meio_ao_volume_estoura_o_teto_sem_travar_a_conciliacao():
    # 300 candidatos diferentes no mesmo dia e um alvo de R$ 300.000,00: fica de fora (teto de
    # trabalho), e os 20 mil sem par do resto do extrato seguem pelo caminho normal.
    rng = random.Random(SEED)
    banco, sistema = _sem_par_espalhados(20_000, 1_000, rng, faixa=(100_000, 199_999))
    dia = date(2024, 1, 1) + timedelta(days=6_000)  # fora das datas do ruído
    alvo = LancamentoParaMatching(uuid.uuid4(), Decimal("-300000.00"), dia, "lote enorme", 1)
    candidatos = [
        LancamentoParaMatching(
            uuid.uuid4(), -Decimal(1_000_003 + 17 * indice) / 100, dia, f"c{indice}", 1
        )
        for indice in range(300)
    ]

    inicio = time.perf_counter()
    resultados = agrupar_por_soma([*banco, alvo], [*sistema, *candidatos], 0)
    duracao = time.perf_counter() - inicio

    assert alvo.id in {
        r.lancamento_banco_id for r in resultados if r.status == "sem_correspondencia"
    }
    assert duracao < TETO_ABSOLUTO_SEGUNDOS
