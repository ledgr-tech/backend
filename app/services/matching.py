"""Motor de matching (issues #16, #22, #23, #24 e #131): concilia os
lançamentos de um extrato do banco com os de um extrato do sistema, pela regra
da ADR-006.

Dividido em camadas, pra que a regra seja testável sem banco:
- `parear_lancamentos`: núcleo puro do motor exato (ADR-006), com o
  pareamento dentro de cada grupo decidido por similaridade de descrição
  quando há ambiguidade (issue #23, ADR-008). Recebe duas listas de
  lançamentos em memória e devolve a lista de resultados, sem tocar em banco.
- `parear_por_tolerancia`: núcleo puro da segunda passada (issue #22, ADR-008),
  sobre o que o motor exato deixou `sem_correspondencia`.
- `agrupar_por_soma`: núcleo puro do agrupamento N para 1 e 1 para N (issue
  #131, ADR-027), atrás da flag `AGRUPAMENTO_HABILITADO`, sobre o que o exato e
  a tolerância deixaram `sem_correspondencia`.
- `classificar_divergencias`: núcleo puro da terceira passada (issue #24,
  ADR-007/ADR-009), sub-classifica o que ainda sobrou `sem_correspondencia`
  depois das passadas anteriores.
- `executar_motor`: as passadas acima em sequência, sem banco.
- `conciliar_extratos`: carrega os lançamentos (sempre filtrando por
  `empresa_id` e `extrato_id`, ADR-004), chama o núcleo e persiste em
  `conciliacoes` (schema da ADR-007).

Regra (ADR-006): os lançamentos são agrupados pela chave (valor com sinal,
data), em dict, nos dois extratos. Com n do banco e m do sistema, formam-se
min(n, m) pares (`match_exato`). Os excedentes de qualquer lado ficam
`duplicado` se o grupo formou pelo menos um par, e `sem_correspondencia` se
não formou nenhum. Chave presente só num dos lados também é
`sem_correspondencia`. Dentro de um grupo sem ambiguidade (um candidato de
cada lado, ou um lado vazio) o par é o único possível. Com mais de um
candidato em pelo menos um dos lados, quem casa com quem é decidido por
similaridade de descrição (issue #23, ADR-008, ver docstring de
`parear_lancamentos`) — valor e data exatos já bastam pra confirmar o match;
a similaridade só desempata a ambiguidade de quem casa com quem, não entra em
`score_confianca`.

Bucket e custo (issue #19): a chave (valor, data) é o bucket do motor exato.
Cada lançamento cai num dict por chave, então nunca há comparação de todos
contra todos: o custo é linear no número de lançamentos, mais a ordenação
dentro de cada grupo (n log n só no pior caso, um grupo gigante). Os testes de
volume (tests/test_matching_volume.py e tests/test_conciliacoes_volume.py)
guardam esse comportamento.

Tolerância de data (issue #22, ADR-008): segunda passada, só quando
`Configuracao.tolerancia_dias_default` da empresa é maior que 0 (default 0
dias corridos, sem linha de Configuracao também vale 0; sem calendário de dia
útil/feriado). Recebe apenas os `sem_correspondencia` da passada exata; os
`duplicado` são decisão exclusiva da ADR-006 e nunca entram. Gera os pares
candidatos (mesmo valor, diferença de datas <= tolerância), ordena por chave
determinística (diferença de dias, datas, descrições normalizadas,
ocorrências) e casa greedy 1:1: `match_tolerancia`, regra `tolerancia`, score
1 - dias/(tolerância+1). A geração de candidatos é por valor (bucket por
valor), então não compara todos contra todos, só os de mesmo valor. O
desempate de candidatos com a mesma diferença de dias usa similaridade de
descrição (issue #23) em vez de comparar a descrição como string simples.

Agrupamento (issue #131, ADR-027): etapa depois do exato e da tolerância e
antes da classificação, só sobre o que sobrou `sem_correspondencia` (nunca
`duplicado`), e roda também com tolerância 0. Um lançamento único de um lado
fecha grupo com 2 ou mais do outro quando a soma bate ao centavo
(`match_agrupado`, regra `agrupamento`, uma linha por lançamento, todas com o
mesmo `grupo_id`). A janela é a tolerância de data da empresa em relação ao
lançamento único; sinal igual em todo o grupo; sem teto de tamanho. Só agrupa
se o conjunto de valores for único (ambiguidade real não agrupa); valores
iguais são intercambiáveis e o desempate é determinístico (menor desvio de
data, descrição mais parecida, ordem estável). Os dois sentidos rodam juntos,
o lançamento único de maior valor primeiro, e a etapa se repete até nada mudar.
O 1 para 1 tem prioridade: se o banco tem um pagamento individual igual a um
lançamento do lote e o sistema não tem lançamento próprio para ele, o 1 para 1
consome o do lote e o grupo não fecha. A busca está em
app/services/soma_exata.py, com o teto de trabalho que a protege de janela
patológica. Tarifa do banco, valor zero e `duplicado` ficam de fora. Ver
`agrupar_por_soma`.

Similaridade de descrição (issue #23, ADR-008): `_similaridade_descricao` é
max(`rapidfuzz.fuzz.token_sort_ratio`, `fuzz.token_set_ratio`) entre duas
descrições — tolera palavras reordenadas (`token_sort_ratio`) e descrições
truncadas/com palavras a mais de um lado (`token_set_ratio`). Usada em dois
lugares: (1) dentro de `parear_lancamentos`, pra decidir quem casa com quem
quando um grupo de mesmo valor+data tem mais de um candidato em algum dos
lados — o pareamento vira guloso por similaridade decrescente, com o
desempate de hoje (descrição normalizada, ocorrência) só entrando quando a
similaridade empata; (2) como critério de desempate em `parear_por_tolerancia`
entre candidatos com a mesma diferença de dias. Em nenhum dos dois casos a
similaridade entra em `score_confianca` ou muda `status`/`regra_aplicada` —
valor+data exatos (ou dentro da tolerância) já são a confirmação do match, a
similaridade só resolve qual combinação é a certa. Em `parear_lancamentos`,
o grupo (mesmo valor+data) que tiver mais de `LIMITE_CANDIDATOS_PARA_
SIMILARIDADE` candidatos num dos lados volta pro pareamento posicional só
pra ele — cap de escala, não de produto, e por grupo, não pra conciliação
inteira; os outros grupos da mesma chamada continuam usando similaridade
normalmente (custo O(n·m) da comparação de todas as combinações não escala
pra grupo gigante — ver docstring da constante).

Classificação de divergências (issue #24, ADR-007 reserva os status desde
`Conciliacao`, ADR-009 decide o que entra em cada categoria): terceira
passada, sobre o que sobrou `sem_correspondencia` depois do motor exato e da
tolerância (quando houver) — nunca sobre `duplicado`, que é decisão exclusiva
da ADR-006 e não passa por aqui. `classificar_divergencias` sub-classifica
cada lançamento restante, de cada lado, em uma das 5 categorias de
divergência (as duas de match, `match_exato`/`match_tolerancia`, não contam
como divergência):
- `tarifa_bancaria`: descrição normalizada contém algum termo de
  `TERMOS_TARIFA_BANCARIA` — só do lado banco, tarifa não existe do lado
  sistema (ERP não lança tarifa que o banco cobrou).
- `divergente_valor`: existe lançamento do outro lado na mesma data — achou
  algo nesse dia, o valor é que não bate.
- `divergente_data`: existe lançamento do outro lado com o mesmo valor —
  achou o valor em outro lugar, a data é que não bate.
- `sem_correspondencia`: nenhum dos anteriores — não achou nada parecido.
Prioridade fixa (tarifa > divergente_valor > divergente_data): quando um
lançamento seria elegível pra mais de uma categoria, a primeira da lista
ganha. Parcelamento e transferência entre contas (ADR-009) não ganham
categoria própria nesta versão, ficam em `sem_correspondencia` como hoje —
só tarifa bancária tem tratamento especial. Custo linear: um set de datas e
um set de valores por lado, montados uma vez antes do loop, então cada
lançamento é O(1) pra checar os dois lados (nunca compara todos contra
todos). `regra_aplicada` fica NULL nas 3 categorias novas, igual já era pra
`sem_correspondencia`/`duplicado` — não é um par decidido por uma regra, é
classificação de item solto.

Não faz log de descrição, valor nem qualquer dado de lançamento (auditoria
de log fica pra Sprint 7). O único log do motor é o do teto de trabalho do
agrupamento, só com id do lançamento e contagem de candidatos.
"""

import logging
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Lancamento
from app.services import soma_exata
from app.services.normalizacao import _normalizar_descricao
from app.services.travas import travar_extrato_banco

logger = logging.getLogger(__name__)

STATUS_MATCH_EXATO = "match_exato"
STATUS_DUPLICADO = "duplicado"
STATUS_SEM_CORRESPONDENCIA = "sem_correspondencia"
STATUS_MATCH_TOLERANCIA = "match_tolerancia"
STATUS_MATCH_AGRUPADO = "match_agrupado"
STATUS_TARIFA_BANCARIA = "tarifa_bancaria"
STATUS_DIVERGENTE_VALOR = "divergente_valor"
STATUS_DIVERGENTE_DATA = "divergente_data"
REGRA_EXATO = "exato"
REGRA_TOLERANCIA = "tolerancia"
REGRA_AGRUPAMENTO = "agrupamento"
SCORE_EXATO = Decimal("1.000")
# Agrupar nunca pontua como par exato (ADR-027): mesmo com todos os lançamentos
# do grupo na mesma data, o score sai no máximo 0,900.
SCORE_MAXIMO_AGRUPAMENTO = Decimal("0.900")
# Teto de trabalho da conciliação inteira no agrupamento, na unidade de
# `soma_exata.LIMITE_TRABALHO` (~2 ns por palavra, uns 4 s). Protege a requisição
# de um extrato com milhares de janelas pesadas, cada uma abaixo do teto por
# lançamento. Os lançamentos únicos de maior valor vêm primeiro, então o lote
# grande de verdade é avaliado antes de qualquer sobra pequena. Estourado, o
# resto da etapa não faz mais busca pesada (o que não precisa dela segue).
LIMITE_TRABALHO_TOTAL = 2_000_000_000
# Quantos lançamentos com o teto de trabalho estourado o motor lista no log por
# conciliação; o resto vai numa linha só, com a contagem.
LIMITE_LOGS_DO_AGRUPAMENTO = 20

# Termos de descrição (já passados por _normalizar_descricao: casefold, sem
# stripar acento de propósito) que classificam um lançamento do banco sem par
# como tarifa bancária (issue #24). Ponto de partida, não lista fechada — vale
# revisar/ampliar com base em extratos reais de bancos diferentes.
TERMOS_TARIFA_BANCARIA = (
    "tarifa",
    "tar pacote",
    "manutencao de conta",
    "manutenção de conta",
    "cesta de servicos",
    "cesta de serviços",
    "anuidade",
    "iof",
    "pacote de servicos",
    "pacote de serviços",
    "taxa de manutencao",
    "taxa de manutenção",
)

# Cap de ESCALA, não de produto — não confundir com os thresholds de
# similaridade/tolerância da ADR-008 (esses decidem SE/COMO um par casa; este
# aqui só decide se vale a pena comparar todas as combinações de um grupo).
# Acima desse número de candidatos num dos lados de UM grupo (mesmo
# valor+data), comparar todas as combinações banco x sistema desse grupo
# vira O(n·m) — com milhares de candidatos dos dois lados isso não termina em
# tempo hábil (ver tests/test_matching_volume.py::
# test_grupo_gigante_com_a_mesma_chave_gera_20k_matches_dentro_do_teto, 20k de
# cada lado, motivo original da issue #19: custo linear no total de
# lançamentos). Grupos assim são o caso patológico desse teste de volume, não
# um cenário de ambiguidade genuína de poucas transações. O fallback é só
# para O GRUPO que estourou o cap — cada grupo é decidido independentemente
# em `parear_lancamentos` (ver `_parear_grupo`), então um único grupo gigante
# num extrato não desliga a similaridade pros outros grupos da mesma
# conciliação.
LIMITE_CANDIDATOS_PARA_SIMILARIDADE = 200


@dataclass(frozen=True)
class LancamentoParaMatching:
    """Recorte mínimo de um lançamento que o motor precisa."""

    id: uuid.UUID
    valor: Decimal
    data: date
    descricao: str
    ocorrencia: int


@dataclass(frozen=True)
class ResultadoMatching:
    """Uma linha futura de `conciliacoes`. Nos excedentes e sem par, só o
    lado correspondente tem id, e regra/score ficam nulos.

    `grupo_id` (issue #131, ADR-027) só vem preenchido nas linhas
    `match_agrupado`: é o ÍNDICE do grupo na execução (0, 1, 2...), igual em
    todas as linhas do mesmo grupo. O núcleo puro só identifica os grupos;
    `conciliar_extratos` troca cada índice por um uuid4 ao gravar."""

    status: str
    lancamento_banco_id: uuid.UUID | None
    lancamento_sistema_id: uuid.UUID | None
    regra_aplicada: str | None = None
    score_confianca: Decimal | None = None
    grupo_id: int | None = None


def _agrupar(
    lancamentos: list[LancamentoParaMatching],
) -> dict[tuple[Decimal, date], list[LancamentoParaMatching]]:
    grupos: dict[tuple[Decimal, date], list[LancamentoParaMatching]] = defaultdict(list)
    for lancamento in lancamentos:
        grupos[(lancamento.valor, lancamento.data)].append(lancamento)
    return grupos


def _ordenar(grupo: list[LancamentoParaMatching]) -> list[LancamentoParaMatching]:
    # sorted é estável: empates completos mantêm a ordem de entrada.
    return sorted(grupo, key=lambda item: (_normalizar_descricao(item.descricao), item.ocorrencia))


def _similaridade_descricao(descricao_a: str, descricao_b: str) -> float:
    """max(token_sort_ratio, token_set_ratio) do rapidfuzz (issue #23,
    ADR-008): token_sort_ratio tolera palavras em ordem diferente
    ("TED MARIA OLIVEIRA" x "OLIVEIRA MARIA TED"), token_set_ratio tolera
    palavras a mais/truncamento de um dos lados ("PIX RECEBIDO JOAO DA
    SILVA" x "JOAO SILVA")."""
    return max(
        fuzz.token_sort_ratio(descricao_a, descricao_b),
        fuzz.token_set_ratio(descricao_a, descricao_b),
    )


def _parear_grupo(
    lado_banco: list[LancamentoParaMatching], lado_sistema: list[LancamentoParaMatching]
) -> list[tuple[LancamentoParaMatching, LancamentoParaMatching]]:
    """Pares dentro de um grupo de mesmo valor+data (ADR-006), em ordem
    determinística. Sem ambiguidade (um candidato de cada lado, ou um lado
    vazio) o par é o único possível. Com mais de um candidato em algum dos
    lados (issue #23), guloso por similaridade de descrição decrescente,
    desempatado por (descrição normalizada, ocorrência) de cada lado — o
    mesmo desempate de antes, que só decide quando a similaridade empata
    (ex: descrições idênticas, sempre 100). Acima de
    `LIMITE_CANDIDATOS_PARA_SIMILARIDADE` candidatos num dos lados, volta pro
    pareamento posicional (ver docstring da constante)."""
    sem_ambiguidade = len(lado_banco) <= 1 and len(lado_sistema) <= 1
    grupo_grande_demais = (
        len(lado_banco) > LIMITE_CANDIDATOS_PARA_SIMILARIDADE
        or len(lado_sistema) > LIMITE_CANDIDATOS_PARA_SIMILARIDADE
    )
    if sem_ambiguidade or grupo_grande_demais:
        return list(zip(lado_banco, lado_sistema, strict=False))

    # índice de cada lado entra como último desempate, só pra garantir uma
    # ordem total mesmo no caso (não esperado em dado real) de dois itens
    # idênticos em descrição normalizada e ocorrência do mesmo lado — sem
    # isso, a comparação cairia nos próprios objetos, que não têm ordem.
    combinacoes = sorted(
        (
            (
                -_similaridade_descricao(do_banco.descricao, do_sistema.descricao),
                _normalizar_descricao(do_banco.descricao),
                do_banco.ocorrencia,
                _normalizar_descricao(do_sistema.descricao),
                do_sistema.ocorrencia,
                indice_banco,
                indice_sistema,
            ),
            do_banco,
            do_sistema,
        )
        for indice_banco, do_banco in enumerate(lado_banco)
        for indice_sistema, do_sistema in enumerate(lado_sistema)
    )

    pares: list[tuple[LancamentoParaMatching, LancamentoParaMatching]] = []
    usados_banco: set[uuid.UUID] = set()
    usados_sistema: set[uuid.UUID] = set()
    for _, do_banco, do_sistema in combinacoes:
        if do_banco.id in usados_banco or do_sistema.id in usados_sistema:
            continue
        usados_banco.add(do_banco.id)
        usados_sistema.add(do_sistema.id)
        pares.append((do_banco, do_sistema))
    return pares


def parear_lancamentos(
    banco: list[LancamentoParaMatching], sistema: list[LancamentoParaMatching]
) -> list[ResultadoMatching]:
    """Núcleo puro do motor exato (ADR-006, com o desempate de pareamento por
    similaridade da issue #23). Determinístico: a mesma entrada gera sempre o
    mesmo pareamento, e os grupos saem em ordem de (data, valor)."""
    grupos_banco = _agrupar(banco)
    grupos_sistema = _agrupar(sistema)
    chaves = sorted(set(grupos_banco) | set(grupos_sistema), key=lambda c: (c[1], c[0]))

    resultados: list[ResultadoMatching] = []
    for chave in chaves:
        lado_banco = _ordenar(grupos_banco.get(chave, []))
        lado_sistema = _ordenar(grupos_sistema.get(chave, []))
        pares = _parear_grupo(lado_banco, lado_sistema)
        usados_banco = {do_banco.id for do_banco, _ in pares}
        usados_sistema = {do_sistema.id for _, do_sistema in pares}

        for do_banco, do_sistema in pares:
            resultados.append(
                ResultadoMatching(
                    status=STATUS_MATCH_EXATO,
                    lancamento_banco_id=do_banco.id,
                    lancamento_sistema_id=do_sistema.id,
                    regra_aplicada=REGRA_EXATO,
                    score_confianca=SCORE_EXATO,
                )
            )

        status_excedente = STATUS_DUPLICADO if pares else STATUS_SEM_CORRESPONDENCIA
        for excedente in lado_banco:
            if excedente.id not in usados_banco:
                resultados.append(ResultadoMatching(status_excedente, excedente.id, None))
        for excedente in lado_sistema:
            if excedente.id not in usados_sistema:
                resultados.append(ResultadoMatching(status_excedente, None, excedente.id))
    return resultados


def parear_por_tolerancia(
    banco_sem_par: list[LancamentoParaMatching],
    sistema_sem_par: list[LancamentoParaMatching],
    tolerancia_dias: int,
) -> list[ResultadoMatching]:
    """Segunda passada (issue #22, ADR-008) sobre os `sem_correspondencia` da
    passada exata. Pura e determinística; ver docstring do módulo."""

    def _sem_correspondencia() -> list[ResultadoMatching]:
        return [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, item.id, None) for item in banco_sem_par
        ] + [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, None, item.id) for item in sistema_sem_par
        ]

    if tolerancia_dias <= 0 or not banco_sem_par or not sistema_sem_par:
        return _sem_correspondencia()

    sistema_por_valor: dict[Decimal, list[LancamentoParaMatching]] = defaultdict(list)
    for do_sistema in sistema_sem_par:
        sistema_por_valor[do_sistema.valor].append(do_sistema)

    candidatos = []
    for do_banco in banco_sem_par:
        for do_sistema in sistema_por_valor.get(do_banco.valor, []):
            dias = abs((do_banco.data - do_sistema.data).days)
            if dias <= tolerancia_dias:
                candidatos.append(
                    (
                        (
                            dias,
                            -_similaridade_descricao(do_banco.descricao, do_sistema.descricao),
                            do_banco.data,
                            do_sistema.data,
                            _normalizar_descricao(do_banco.descricao),
                            do_banco.ocorrencia,
                            _normalizar_descricao(do_sistema.descricao),
                            do_sistema.ocorrencia,
                        ),
                        do_banco,
                        do_sistema,
                    )
                )
    candidatos.sort(key=lambda candidato: candidato[0])

    resultados: list[ResultadoMatching] = []
    usados_banco: set[uuid.UUID] = set()
    usados_sistema: set[uuid.UUID] = set()
    for chave, do_banco, do_sistema in candidatos:
        if do_banco.id in usados_banco or do_sistema.id in usados_sistema:
            continue
        usados_banco.add(do_banco.id)
        usados_sistema.add(do_sistema.id)
        resultados.append(
            ResultadoMatching(
                status=STATUS_MATCH_TOLERANCIA,
                lancamento_banco_id=do_banco.id,
                lancamento_sistema_id=do_sistema.id,
                regra_aplicada=REGRA_TOLERANCIA,
                score_confianca=round(
                    Decimal(1) - Decimal(chave[0]) / Decimal(tolerancia_dias + 1), 3
                ),
            )
        )

    for item in banco_sem_par:
        if item.id not in usados_banco:
            resultados.append(ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, item.id, None))
    for item in sistema_sem_par:
        if item.id not in usados_sistema:
            resultados.append(ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, None, item.id))
    return resultados


@dataclass(frozen=True)
class _Ponta:
    """Um lançamento sem par elegível ao agrupamento, com o valor em centavos
    inteiros (`centavos`, sempre positivo) e o sinal à parte."""

    item: LancamentoParaMatching
    indice: int  # posição entre todas as pontas da etapa, pra marcar uso sem hash de uuid
    lado: str  # "banco" ou "sistema"
    sinal: int
    centavos: int
    ordinal: int
    # Desempate estável e total (descrição normalizada, ocorrência, posição na
    # entrada), sem depender de objeto nem de id.
    chave: tuple[str, int, int]


def _centavos(valor: Decimal) -> int | None:
    """O valor absoluto em centavos inteiros, ou `None` se for zero ou não
    cair num número inteiro de centavos (a soma é ao centavo, sem arredondar)."""
    centavos = abs(valor) * 100
    inteiro = centavos.to_integral_value()
    if centavos != inteiro or inteiro <= 0:
        return None
    return int(inteiro)


def _pontas_do_agrupamento(
    itens: list[LancamentoParaMatching], lado: str, primeiro_indice: int
) -> list[_Ponta]:
    """Os lançamentos que podem entrar num grupo: valor diferente de zero e,
    do lado do banco, sem termo de tarifa (a tarifa mantém a prioridade da
    classificação, ADR-009)."""
    pontas = []
    for indice, item in enumerate(itens):
        centavos = _centavos(item.valor)
        if centavos is None:
            continue
        descricao = _normalizar_descricao(item.descricao)
        if lado == "banco" and _contem_termo_tarifa(descricao):
            continue
        pontas.append(
            _Ponta(
                item=item,
                indice=primeiro_indice + len(pontas),
                lado=lado,
                sinal=1 if item.valor > 0 else -1,
                centavos=centavos,
                ordinal=item.data.toordinal(),
                chave=(descricao, item.ocorrencia, indice),
            )
        )
    return pontas


def _escolher_membros(
    unico: _Ponta, candidatos: list[_Ponta], quantidades: dict[int, int]
) -> list[_Ponta]:
    """Os candidatos de cada valor do conjunto achado. Candidatos de mesmo
    valor são equivalentes pra soma, então entram os que desviam menos em data
    do lançamento único, depois os de descrição mais parecida com a dele
    (`_similaridade_descricao`), depois a ordem estável. O desvio total e a
    similaridade total são somas por candidato, então escolher valor a valor
    dá o mesmo conjunto que escolher o grupo inteiro."""
    por_valor: dict[int, list[_Ponta]] = defaultdict(list)
    for candidato in candidatos:
        por_valor[candidato.centavos].append(candidato)

    membros: list[_Ponta] = []
    for centavos, quantidade in quantidades.items():
        do_valor = por_valor[centavos]
        if len(do_valor) > quantidade:
            do_valor = sorted(
                do_valor,
                key=lambda c: (
                    abs(c.ordinal - unico.ordinal),
                    -_similaridade_descricao(c.item.descricao, unico.item.descricao),
                    c.chave,
                ),
            )
        membros.extend(do_valor[:quantidade])
    return sorted(membros, key=lambda c: (c.ordinal, c.centavos, c.chave))


def agrupar_por_soma(
    banco_sem_par: list[LancamentoParaMatching],
    sistema_sem_par: list[LancamentoParaMatching],
    tolerancia_dias: int,
) -> list[ResultadoMatching]:
    """Quarta etapa (issue #131, ADR-027) sobre os `sem_correspondencia` das
    passadas anteriores: agrupamento N para 1 e 1 para N, `match_agrupado`,
    regra `agrupamento`. Pura e determinística, no contrato de
    `parear_por_tolerancia`: recebe os sem par e devolve resultados pra todos
    eles, os que entraram em grupo e os que continuam `sem_correspondencia`.

    Um lançamento único de um lado fecha grupo com 2 ou mais lançamentos do
    outro quando a soma bate ao centavo. Os candidatos dele são os lançamentos
    sem par do outro lado, do mesmo sinal, de valor diferente de zero e menor
    que o dele, com data a no máximo `tolerancia_dias` da dele (a mesma medida
    do 1 para 1: os membros ficam a até 2x a tolerância entre si; com
    tolerância 0, só o mesmo dia). Lançamento do banco com termo de tarifa
    fica de fora. A busca é completa e exata (app/services/soma_exata.py), sem
    teto de tamanho do grupo, e só agrupa se o conjunto de valores for único:
    com dois conjuntos de valores diferentes e a mesma soma, é ambíguo e não
    agrupa. Conjuntos de mesmo multiconjunto de valores não são ambiguidade
    (`_escolher_membros`).

    Os dois sentidos rodam juntos. A ordem é determinística: o lançamento
    único de maior valor absoluto primeiro, desempate por chave estável, e um
    lançamento entra num grupo só, como único ou como membro. A etapa se
    repete até nada mudar, porque um grupo formado consome candidatos e pode
    desfazer a ambiguidade de outro (os únicos sem solução, que só ficam sem
    candidatos, não voltam a ser avaliados). O pareamento 1 para 1 já rodou e
    tem prioridade: o que ele consumiu não está aqui.

    O teto de trabalho (`soma_exata.LIMITE_TRABALHO`) é só proteção contra
    janela patológica: estourou, o lançamento segue como divergência, e o log
    leva só ids e contagens. `score_confianca` é 1 - maior desvio de data /
    (tolerância + 1), limitado a `SCORE_MAXIMO_AGRUPAMENTO`; `grupo_id` é o
    índice do grupo (ver `ResultadoMatching`).
    """

    def _sem_correspondencia(usados: set[uuid.UUID]) -> list[ResultadoMatching]:
        return [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, item.id, None)
            for item in banco_sem_par
            if item.id not in usados
        ] + [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, None, item.id)
            for item in sistema_sem_par
            if item.id not in usados
        ]

    tolerancia = max(tolerancia_dias or 0, 0)
    pontas = _pontas_do_agrupamento(banco_sem_par, "banco", 0)
    pontas += _pontas_do_agrupamento(sistema_sem_par, "sistema", len(pontas))
    if not banco_sem_par or not sistema_sem_par or len(pontas) < 3:
        return _sem_correspondencia(set())

    # Por lado e sinal, em ordem de data, pra achar a janela com bisect.
    por_lado_e_sinal: dict[tuple[str, int], list[_Ponta]] = defaultdict(list)
    for ponta in sorted(pontas, key=lambda p: (p.ordinal, p.centavos, p.chave)):
        por_lado_e_sinal[(ponta.lado, ponta.sinal)].append(ponta)
    ordinais = {
        lado_e_sinal: [ponta.ordinal for ponta in lista]
        for lado_e_sinal, lista in por_lado_e_sinal.items()
    }

    usada = [False] * len(pontas)  # por `_Ponta.indice`

    def _candidatos(unico: _Ponta) -> list[_Ponta]:
        lado_oposto = "sistema" if unico.lado == "banco" else "banco"
        lista = por_lado_e_sinal.get((lado_oposto, unico.sinal), [])
        datas = ordinais.get((lado_oposto, unico.sinal), [])
        inicio = bisect_left(datas, unico.ordinal - tolerancia)
        fim = bisect_right(datas, unico.ordinal + tolerancia)
        limite = unico.centavos
        return [
            candidato
            for candidato in lista[inicio:fim]
            if candidato.centavos < limite and not usada[candidato.indice]
        ]

    # Em que versão do conjunto de candidatos cada data de cada lado e sinal
    # foi mexida por último (a versão sobe a cada grupo formado), e em que
    # versão cada lançamento único foi avaliado. Um único ambíguo ou estourado
    # só é avaliado de novo se algum candidato da janela dele foi consumido
    # desde então: sem isso, a repetição reavaliaria tudo a cada rodada.
    versao = 0
    alterado_em: dict[tuple[str, int], dict[int, int]] = defaultdict(dict)
    avaliado_na_versao: dict[int, int] = {}

    def _janela_mudou(unico: _Ponta) -> bool:
        lado_oposto = "sistema" if unico.lado == "banco" else "banco"
        mexidas = alterado_em.get((lado_oposto, unico.sinal))
        if not mexidas:
            return False
        desde = avaliado_na_versao[unico.indice]
        return any(
            mexidas.get(dia, -1) > desde
            for dia in range(unico.ordinal - tolerancia, unico.ordinal + tolerancia + 1)
        )

    # Maior valor absoluto primeiro; o lado, a data e a chave estável desempatam.
    pendentes = sorted(pontas, key=lambda p: (-p.centavos, p.lado, p.ordinal, p.chave))
    grupos: list[tuple[_Ponta, list[_Ponta]]] = []
    estourados: dict[int, int] = {}  # por `_Ponta.indice`, com a contagem de candidatos
    trabalho_restante = LIMITE_TRABALHO_TOTAL
    while pendentes:
        seguintes = []
        formou = False
        for unico in pendentes:
            if usada[unico.indice]:
                continue
            if unico.indice in avaliado_na_versao and not _janela_mudou(unico):
                seguintes.append(unico)
                continue
            avaliado_na_versao[unico.indice] = versao
            estourados.pop(unico.indice, None)
            candidatos = _candidatos(unico)
            if len(candidatos) < 2:
                continue
            busca = soma_exata.buscar_conjunto_unico(
                unico.centavos,
                [candidato.centavos for candidato in candidatos],
                limite_trabalho=min(soma_exata.LIMITE_TRABALHO, trabalho_restante),
            )
            trabalho_restante -= busca.trabalho
            if busca.situacao == soma_exata.UNICO:
                membros = _escolher_membros(unico, candidatos, busca.quantidades or {})
                grupos.append((unico, membros))
                versao += 1
                for ponta in [unico, *membros]:
                    usada[ponta.indice] = True
                    alterado_em[(ponta.lado, ponta.sinal)][ponta.ordinal] = versao
                formou = True
            elif busca.situacao in (soma_exata.AMBIGUO, soma_exata.ESTOUROU):
                # Pode mudar quando um grupo consumir candidatos dele.
                seguintes.append(unico)
                if busca.situacao == soma_exata.ESTOUROU:
                    estourados[unico.indice] = len(candidatos)
        if not formou:
            break
        pendentes = seguintes

    _registrar_tetos_estourados(
        [(pontas[indice].item.id, candidatos) for indice, candidatos in estourados.items()]
    )

    resultados: list[ResultadoMatching] = []
    for indice, (unico, membros) in enumerate(grupos):
        desvio_maximo = max(abs(membro.ordinal - unico.ordinal) for membro in membros)
        score = min(
            round(Decimal(1) - Decimal(desvio_maximo) / Decimal(tolerancia + 1), 3),
            SCORE_MAXIMO_AGRUPAMENTO,
        )
        for ponta in [unico, *membros]:
            resultados.append(
                ResultadoMatching(
                    status=STATUS_MATCH_AGRUPADO,
                    lancamento_banco_id=ponta.item.id if ponta.lado == "banco" else None,
                    lancamento_sistema_id=ponta.item.id if ponta.lado == "sistema" else None,
                    regra_aplicada=REGRA_AGRUPAMENTO,
                    score_confianca=score,
                    grupo_id=indice,
                )
            )
    agrupados = {r.lancamento_banco_id or r.lancamento_sistema_id for r in resultados}
    return resultados + _sem_correspondencia(agrupados)


def _registrar_tetos_estourados(estourados: list[tuple[uuid.UUID, int]]) -> None:
    """Loga os lançamentos únicos cuja busca passou do teto de trabalho e que
    seguem sem grupo: só o id do lançamento e a contagem de candidatos, nunca
    valor, descrição ou data (e no máximo `LIMITE_LOGS_DO_AGRUPAMENTO` linhas
    por conciliação, o resto numa contagem só)."""
    for id_, candidatos in estourados[:LIMITE_LOGS_DO_AGRUPAMENTO]:
        logger.warning(
            "agrupamento resultado=teto_de_trabalho lancamento_id=%s candidatos=%d",
            id_,
            candidatos,
        )
    if len(estourados) > LIMITE_LOGS_DO_AGRUPAMENTO:
        logger.warning(
            "agrupamento resultado=teto_de_trabalho outros_lancamentos=%d",
            len(estourados) - LIMITE_LOGS_DO_AGRUPAMENTO,
        )


def _contem_termo_tarifa(descricao_normalizada: str) -> bool:
    return any(termo in descricao_normalizada for termo in TERMOS_TARIFA_BANCARIA)


def _classificar_remanescente(
    item: LancamentoParaMatching,
    datas_do_outro_lado: set[date],
    valores_do_outro_lado: set[Decimal],
    permitir_tarifa: bool,
) -> str:
    """Prioridade fixa: tarifa (só quando `permitir_tarifa`, ou seja, lado
    banco) > divergente_valor > divergente_data > sem_correspondencia."""
    if permitir_tarifa and _contem_termo_tarifa(_normalizar_descricao(item.descricao)):
        return STATUS_TARIFA_BANCARIA
    if item.data in datas_do_outro_lado:
        return STATUS_DIVERGENTE_VALOR
    if item.valor in valores_do_outro_lado:
        return STATUS_DIVERGENTE_DATA
    return STATUS_SEM_CORRESPONDENCIA


def classificar_divergencias(
    banco_restantes: list[LancamentoParaMatching],
    sistema_restantes: list[LancamentoParaMatching],
) -> list[ResultadoMatching]:
    """Terceira passada (issue #24, ADR-007/ADR-009) sobre o que sobrou
    `sem_correspondencia` das passadas anteriores. Pura; ver docstring do
    módulo. `regra_aplicada`/`score_confianca` ficam NULL: classificação de
    item solto, não um par decidido por regra."""
    datas_sistema = {item.data for item in sistema_restantes}
    valores_sistema = {item.valor for item in sistema_restantes}
    datas_banco = {item.data for item in banco_restantes}
    valores_banco = {item.valor for item in banco_restantes}

    resultados = [
        ResultadoMatching(
            _classificar_remanescente(item, datas_sistema, valores_sistema, permitir_tarifa=True),
            item.id,
            None,
        )
        for item in banco_restantes
    ]
    resultados += [
        ResultadoMatching(
            _classificar_remanescente(item, datas_banco, valores_banco, permitir_tarifa=False),
            None,
            item.id,
        )
        for item in sistema_restantes
    ]
    return resultados


def _carregar_lancamentos(
    db: Session, empresa_id: uuid.UUID, extrato_id: uuid.UUID
) -> list[LancamentoParaMatching]:
    linhas = db.execute(
        select(
            Lancamento.id,
            Lancamento.valor,
            Lancamento.data,
            Lancamento.descricao,
            Lancamento.ocorrencia,
        ).where(Lancamento.empresa_id == empresa_id, Lancamento.extrato_id == extrato_id)
    ).all()
    return [
        LancamentoParaMatching(
            id=linha.id,
            valor=linha.valor,
            data=linha.data,
            descricao=linha.descricao,
            ocorrencia=linha.ocorrencia,
        )
        for linha in linhas
    ]


def _sobras(
    resultados: list[ResultadoMatching],
    banco_por_id: dict[uuid.UUID, LancamentoParaMatching],
    sistema_por_id: dict[uuid.UUID, LancamentoParaMatching],
) -> tuple[list[LancamentoParaMatching], list[LancamentoParaMatching]]:
    """Os LancamentoParaMatching originais por trás de cada
    ResultadoMatching(status=sem_correspondencia) de `resultados`."""
    sem_par = [r for r in resultados if r.status == STATUS_SEM_CORRESPONDENCIA]
    return (
        [banco_por_id[r.lancamento_banco_id] for r in sem_par if r.lancamento_banco_id],
        [sistema_por_id[r.lancamento_sistema_id] for r in sem_par if r.lancamento_sistema_id],
    )


def executar_motor(
    banco: list[LancamentoParaMatching],
    sistema: list[LancamentoParaMatching],
    tolerancia_dias: int,
    agrupar: bool,
) -> list[ResultadoMatching]:
    """As passadas do motor em sequência, sem banco: exato (ADR-006),
    tolerância de data (só com `tolerancia_dias` > 0), agrupamento (só com
    `agrupar`, a flag `AGRUPAMENTO_HABILITADO`; roda também com tolerância 0) e
    a classificação das divergências, nessa ordem. Cada passada só vê o que a
    anterior deixou `sem_correspondencia`, nunca o `duplicado`. É o que
    `conciliar_extratos` persiste."""
    resultados = parear_lancamentos(banco, sistema)
    banco_por_id = {item.id: item for item in banco}
    sistema_por_id = {item.id: item for item in sistema}

    if tolerancia_dias:
        banco_sem_par, sistema_sem_par = _sobras(resultados, banco_por_id, sistema_por_id)
        resultados = [r for r in resultados if r.status != STATUS_SEM_CORRESPONDENCIA]
        resultados += parear_por_tolerancia(banco_sem_par, sistema_sem_par, tolerancia_dias)

    # Agrupamento (issue #131): sobre o que o 1 para 1 deixou, que tem prioridade.
    if agrupar:
        banco_sem_par, sistema_sem_par = _sobras(resultados, banco_por_id, sistema_por_id)
        resultados = [r for r in resultados if r.status != STATUS_SEM_CORRESPONDENCIA]
        resultados += agrupar_por_soma(banco_sem_par, sistema_sem_par, tolerancia_dias)

    # Terceira passada (issue #24): sub-classifica o que ainda sobrou
    # sem_correspondencia depois das anteriores.
    banco_sem_par, sistema_sem_par = _sobras(resultados, banco_por_id, sistema_por_id)
    resultados = [r for r in resultados if r.status != STATUS_SEM_CORRESPONDENCIA]
    resultados += classificar_divergencias(banco_sem_par, sistema_sem_par)
    return resultados


def conciliar_extratos(
    db: Session,
    empresa_id: uuid.UUID,
    extrato_banco_id: uuid.UUID,
    extrato_sistema_id: uuid.UUID,
) -> dict[str, int]:
    """Roda o motor exato pro par de extratos e persiste em `conciliacoes`.

    Idempotente (ADR-007): apaga as conciliações anteriores do par
    (empresa_id, extrato_banco_id, extrato_sistema_id) e grava as novas na
    mesma transação, em insert em lote. Devolve as contagens por status
    (incluindo `match_tolerancia`, issue #27, e `match_agrupado` e
    `agrupamentos`, issue #131 — ver comentário no cálculo de `contagens`
    abaixo). Quem chama valida empresa, origem e status dos extratos antes. O
    agrupamento só roda com `AGRUPAMENTO_HABILITADO` ligada; desligada, o
    resultado é o de antes dele.

    Concorrência: a transação começa adquirindo a trava do EXTRATO DO BANCO
    (`app/services/travas.py`, desde a issue #86; antes era por par), então
    duas execuções simultâneas do mesmo extrato do banco se enfileiram (a
    segunda espera a primeira dar commit e depois apaga e regrava) em vez de
    duplicar linhas, e uma decisão por linha nunca é gravada no meio de uma
    rodada nova, mesmo com outro extrato do sistema. A trava solta sozinha no
    commit ou rollback; extratos do banco diferentes não se bloqueiam. A rota
    pode já ter pegado a mesma trava (e a do mês) antes: é reentrante.

    Histórico (issue #27, ADR-010): grava também uma linha em
    `execucoes_conciliacao`, na mesma transação/lock — ao contrário de
    `conciliacoes`, essa tabela é só de inserção e nunca é apagada, então o
    histórico sobrevive a reconciliações futuras do mesmo par.
    """
    try:
        travar_extrato_banco(db, empresa_id, extrato_banco_id)
        banco = _carregar_lancamentos(db, empresa_id, extrato_banco_id)
        sistema = _carregar_lancamentos(db, empresa_id, extrato_sistema_id)
        tolerancia_dias = db.execute(
            select(Configuracao.tolerancia_dias_default).where(
                Configuracao.empresa_id == empresa_id
            )
        ).scalar_one_or_none()
        resultados = executar_motor(
            banco, sistema, tolerancia_dias or 0, agrupar=settings.agrupamento_habilitado
        )

        # Contagens calculadas uma única vez (issue #27, ADR-010): antes, o
        # retorno do motor não incluía match_tolerancia, então a soma das
        # categorias devolvidas ficava menor que `total` sempre que
        # tolerancia_dias > 0. O mesmo dict grava `execucoes_conciliacao` e
        # volta pro response de `POST /conciliacoes`, pra nunca mais divergir.
        #
        # Agrupamento (issue #131, ADR-027): `match_agrupado` conta LINHAS (os
        # lançamentos que entraram em grupos), pra soma das categorias seguir
        # igual a `total` e o CHECK de `execucoes_conciliacao` continuar
        # valendo; `agrupamentos` conta os grupos e fica fora da soma.
        contagens = {
            "total": len(resultados),
            STATUS_MATCH_EXATO: sum(r.status == STATUS_MATCH_EXATO for r in resultados),
            STATUS_MATCH_TOLERANCIA: sum(r.status == STATUS_MATCH_TOLERANCIA for r in resultados),
            STATUS_MATCH_AGRUPADO: sum(r.status == STATUS_MATCH_AGRUPADO for r in resultados),
            STATUS_DUPLICADO: sum(r.status == STATUS_DUPLICADO for r in resultados),
            STATUS_SEM_CORRESPONDENCIA: sum(
                r.status == STATUS_SEM_CORRESPONDENCIA for r in resultados
            ),
            STATUS_TARIFA_BANCARIA: sum(r.status == STATUS_TARIFA_BANCARIA for r in resultados),
            STATUS_DIVERGENTE_VALOR: sum(r.status == STATUS_DIVERGENTE_VALOR for r in resultados),
            STATUS_DIVERGENTE_DATA: sum(r.status == STATUS_DIVERGENTE_DATA for r in resultados),
            "agrupamentos": len({r.grupo_id for r in resultados if r.grupo_id is not None}),
        }
        # O núcleo puro identifica o grupo por um índice; o uuid4 nasce aqui.
        grupos = {
            indice: uuid.uuid4()
            for indice in sorted({r.grupo_id for r in resultados if r.grupo_id is not None})
        }

        db.execute(
            delete(Conciliacao).where(
                Conciliacao.empresa_id == empresa_id,
                Conciliacao.extrato_banco_id == extrato_banco_id,
                Conciliacao.extrato_sistema_id == extrato_sistema_id,
            )
        )
        if resultados:
            # Core na Table, não `insert(Conciliacao)` do ORM: o bulk insert do ORM
            # quebra o lote a cada mudança no padrão de campos nulos (par casado,
            # só banco, só sistema), gerando muitos statements, e cada um custa
            # uma ida e volta ao banco. Pelo Core tudo vai num único executemany.
            db.execute(
                insert(Conciliacao.__table__),
                [
                    {
                        "id": uuid.uuid4(),
                        "empresa_id": empresa_id,
                        "extrato_banco_id": extrato_banco_id,
                        "extrato_sistema_id": extrato_sistema_id,
                        "lancamento_banco_id": resultado.lancamento_banco_id,
                        "lancamento_sistema_id": resultado.lancamento_sistema_id,
                        "status": resultado.status,
                        "regra_aplicada": resultado.regra_aplicada,
                        "score_confianca": resultado.score_confianca,
                        "grupo_id": grupos.get(resultado.grupo_id),
                    }
                    for resultado in resultados
                ],
            )

        # Histórico da execução (issue #27, ADR-010): uma linha por chamada
        # bem-sucedida, na mesma transação/lock do motor, nunca editada nem
        # apagada depois — ao contrário de `conciliacoes`, que é idempotente.
        db.execute(
            insert(ExecucaoConciliacao.__table__),
            [
                {
                    "id": uuid.uuid4(),
                    "empresa_id": empresa_id,
                    "extrato_banco_id": extrato_banco_id,
                    "extrato_sistema_id": extrato_sistema_id,
                    "tolerancia_dias": tolerancia_dias or 0,
                    **contagens,
                }
            ],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return contagens
