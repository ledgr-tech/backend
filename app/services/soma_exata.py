"""Busca exata de um conjunto de valores que soma o alvo (issue #131, ADR-027).

É o coração do agrupamento N para 1 e 1 para N do motor de matching
(app/services/matching.py): dado o valor do lançamento único e os valores dos
candidatos do outro lado, em centavos inteiros, acha o grupo, ou diz que não há
grupo ou que há mais de um. Pura e sem dependência: só inteiros.

Contrato de `buscar_conjunto_unico(alvo, valores)`, `valores` inteiros
positivos, repetidos à vontade:

- Um "conjunto" é um multiconjunto de VALORES: dois candidatos de mesmo valor
  são intercambiáveis, então trocar um pelo outro não é outro conjunto.
- Só valem conjuntos de 2 ou mais valores. Um valor igual ou maior que o alvo
  nunca entra (sozinho seria 1 para 1, e acompanhado passa do alvo).
- `UNICO`: existe exatamente um conjunto de valores com soma igual ao alvo, e
  `quantidades` diz quantos de cada valor ele tem. `NENHUM`: não existe.
  `AMBIGUO`: existe mais de um, de valores diferentes entre si. `ESTOUROU`: a
  busca passaria do teto de trabalho (`LIMITE_TRABALHO`) e foi abandonada, sem
  resposta: quem chama trata como "não agrupa".
- Exata e sem tamanho máximo de conjunto. Determinística: a resposta depende
  só do alvo e dos valores, não da ordem em que chegam. O teto de trabalho é
  calculado das entradas (nunca do relógio), então `ESTOUROU` também é
  determinístico.

Como busca, sem enumerar 2^n subconjuntos:

1. Atalhos baratos: menos de 2 candidatos, soma de todos menor que o alvo, os
   dois menores já passando do alvo, ou soma de todos igual ao alvo (então é o
   conjunto inteiro, o único possível, porque os valores são positivos).
2. Divide tudo pelo máximo divisor comum dos valores (alvo que não é múltiplo
   dele não tem solução) e procura o menor entre o alvo e o seu complemento
   (soma de todos menos o alvo): um conjunto soma o alvo se e só se o resto
   soma o complemento, e o número de conjuntos distintos é o mesmo.
3. Poucas combinações (`LIMITE_ENUMERACAO`): enumera as quantidades de cada
   valor, com poda, e o tipo de mais cópias sai por divisão exata.
4. Senão, programação dinâmica sobre as somas possíveis, com um inteiro
   gigante do Python como conjunto de bits (o bit `s` ligado diz que a soma
   `s` é alcançável). Cada tipo de valor, com `m` cópias, entra por divisão
   binária (cópias em blocos de 1, 2, 4...), então custa `log m` deslocamentos
   de `alvo / 64` palavras e não `m`. As camadas (somas alcançáveis com os
   primeiros `i` tipos) servem para recuperar a resposta de trás para frente: em
   cada tipo, as quantidades válidas são as que deixam um resto alcançável
   pelas camadas anteriores. Toda quantidade válida leva a pelo menos uma
   solução, então basta olhar: com duas quantidades válidas num tipo já há dois
   conjuntos diferentes (ambíguo), e com uma só se segue em frente. Para a
   memória não crescer com o número de tipos, só se guarda uma camada a cada
   `sqrt(tipos)` e as demais são refeitas bloco a bloco na volta.

Teto de trabalho (`LIMITE_TRABALHO`): é só proteção contra janela patológica
(centenas de candidatos diferentes e alvo grande), não limite de tamanho do
grupo. Mede o custo da programação dinâmica em palavras de 64 bits mexidas
(deslocamentos de ida e de volta vezes o tamanho do alvo). A constante foi
calibrada por medição, com o resultado no comentário dela. `Busca.trabalho`
devolve o custo gasto, pra quem chama somar o de várias buscas (teto da
conciliação inteira, `matching.LIMITE_TRABALHO_TOTAL`).

Não faz log nem guarda nenhum dado de lançamento: recebe só números.
"""

from collections import Counter
from dataclasses import dataclass
from functools import reduce
from math import gcd, isqrt

UNICO = "unico"
NENHUM = "nenhum"
AMBIGUO = "ambiguo"
ESTOUROU = "estourou"

# Até este número de combinações de quantidades (produto de `m + 1` de cada
# tipo, menos o de mais cópias), enumera direto em vez de montar camadas de bits.
LIMITE_ENUMERACAO = 1000

# Quanto vale um passo da enumeração (uma chamada da busca em profundidade, de
# ~1 a 2 µs) na unidade do teto de trabalho (palavra de 64 bits, ~2 ns). Só entra
# em `Busca.trabalho`, pro teto da conciliação inteira: a enumeração em si já é
# limitada por `LIMITE_ENUMERACAO`.
PESO_DO_PASSO_DA_ENUMERACAO = 500

# Teto de trabalho por busca, em palavras de 64 bits mexidas pela programação
# dinâmica. Calibrado em 04/11 (Python 3.13 em Docker, num notebook de
# desenvolvimento): o custo estimado prevê bem o tempo, entre 1,4 e 2,7 ns por
# palavra, e a memória de pico é de uns 35 MB com 150 candidatos diferentes e
# alvo de R$ 150.000,00 e de uns 70 MB no teto. Medidas (candidatos diferentes,
# alvo em R$): 100 e 150 mil levam ~0,12 s; 100 e 300 mil, ~0,19 s; 300 e 300
# mil (281 milhões de palavras), ~0,63 s; 400 e 1,59 milhão (2 bilhões), ~2,9 s.
# O teto de 100 milhões de palavras deixa uns 0,2 s no pior caso aceito, e o
# caso real do cliente (um lote de R$ 150.000,00 contra até uns 200 candidatos
# diferentes na janela) passa. Já "centenas de candidatos e alvo grande" estoura.
LIMITE_TRABALHO = 100_000_000


@dataclass(frozen=True)
class Busca:
    situacao: str
    # valor -> quantas cópias entram no conjunto; só quando `situacao` é UNICO,
    # e só os valores com pelo menos uma cópia.
    quantidades: dict[int, int] | None = None
    # Trabalho gasto, na unidade de `LIMITE_TRABALHO` (palavras mexidas pela
    # programação dinâmica; a enumeração entra pelo peso de cada passo; 0 nos
    # atalhos).
    trabalho: int = 0


def _bit(camada: bytes, posicao: int) -> int:
    return (camada[posicao >> 3] >> (posicao & 7)) & 1


def _acrescentar(camada: int, valor: int, copias: int, mascara: int) -> int:
    """`camada` com as somas de 0 a `copias` cópias de `valor` somadas a cada
    soma que ela já tinha, cortada em `mascara` (as somas até o alvo)."""
    restante, bloco = copias, 1
    while restante > 0:
        passo = min(bloco, restante)
        camada |= (camada << (passo * valor)) & mascara
        restante -= passo
        bloco <<= 1
    return camada


def _enumerar(alvo: int, tipos: list[tuple[int, int]], efetivas: list[int]) -> Busca:
    """Quantidades de cada tipo, por busca em profundidade com poda, até achar
    duas soluções. O tipo de mais cópias fica por último: sobra a ele uma
    divisão exata, não um laço, então o custo é o produto das cópias dos
    demais."""
    ordem = sorted(range(len(tipos)), key=lambda i: (efetivas[i], tipos[i][0]))
    capacidade = [0] * (len(ordem) + 1)  # o que os tipos de `p` em diante ainda somam, no máximo
    for posicao in range(len(ordem) - 1, -1, -1):
        indice = ordem[posicao]
        capacidade[posicao] = capacidade[posicao + 1] + tipos[indice][0] * efetivas[indice]

    achadas: list[dict[int, int]] = []
    passos = 0

    def descer(posicao: int, resto: int, escolhas: dict[int, int]) -> None:
        nonlocal passos
        passos += 1
        indice = ordem[posicao]
        valor = tipos[indice][0]
        if posicao == len(ordem) - 1:
            if resto % valor == 0 and resto // valor <= efetivas[indice]:
                achadas.append({**escolhas, indice: resto // valor})
            return
        menor = max(0, -(-(resto - capacidade[posicao + 1]) // valor))
        for copias in range(menor, min(efetivas[indice], resto // valor) + 1):
            descer(posicao + 1, resto - copias * valor, {**escolhas, indice: copias})
            if len(achadas) >= 2:
                return

    descer(0, alvo, {})
    trabalho = passos * PESO_DO_PASSO_DA_ENUMERACAO
    if not achadas:
        return Busca(NENHUM, trabalho=trabalho)
    if len(achadas) > 1:
        return Busca(AMBIGUO, trabalho=trabalho)
    return Busca(UNICO, {tipos[i][0]: q for i, q in achadas[0].items() if q}, trabalho=trabalho)


def _buscar_denso(
    alvo: int, tipos: list[tuple[int, int]], efetivas: list[int], limite_trabalho: int
) -> Busca:
    # Um tipo com `e` cópias entra em `e.bit_length()` deslocamentos (divisão binária).
    deslocamentos = sum(copias.bit_length() for copias in efetivas)
    trabalho = 2 * deslocamentos * (alvo // 64 + 1)
    if trabalho > limite_trabalho:
        return Busca(ESTOUROU)

    mascara = (1 << (alvo + 1)) - 1
    bloco = max(1, isqrt(len(tipos)))
    pontos = {0: 1}  # camada antes do tipo `i`, guardada a cada `bloco` tipos
    camada = 1
    for indice, ((valor, _), copias) in enumerate(zip(tipos, efetivas, strict=True)):
        camada = _acrescentar(camada, valor, copias, mascara)
        if (indice + 1) % bloco == 0:
            pontos[indice + 1] = camada
    if not (camada >> alvo) & 1:
        return Busca(NENHUM, trabalho=trabalho)

    quantidades = [0] * len(tipos)
    resto = alvo
    fim = len(tipos) - 1
    while fim >= 0:
        inicio = (fim // bloco) * bloco
        camadas = [pontos[inicio]]  # camada antes do tipo `inicio`, `inicio + 1`...
        for indice in range(inicio, fim):
            camadas.append(_acrescentar(camadas[-1], tipos[indice][0], efetivas[indice], mascara))
        for indice in range(fim, inicio - 1, -1):
            valor = tipos[indice][0]
            anterior = camadas[indice - inicio].to_bytes((alvo >> 3) + 1, "little")
            validas = [
                copias
                for copias in range(min(efetivas[indice], resto // valor) + 1)
                if _bit(anterior, resto - copias * valor)
            ]
            if len(validas) > 1:
                return Busca(AMBIGUO, trabalho=trabalho)
            quantidades[indice] = validas[0]
            resto -= validas[0] * valor
        fim = inicio - 1
    return Busca(
        UNICO,
        {valor: q for q, (valor, _) in zip(quantidades, tipos, strict=True) if q},
        trabalho=trabalho,
    )


def buscar_conjunto_unico(
    alvo: int, valores: list[int] | tuple[int, ...], limite_trabalho: int = LIMITE_TRABALHO
) -> Busca:
    """Ver a docstring do módulo."""
    uteis = [valor for valor in valores if 0 < valor < alvo]
    if len(uteis) < 2:
        return Busca(NENHUM)
    soma_total = sum(uteis)
    if soma_total < alvo:
        return Busca(NENHUM)
    uteis.sort()
    if uteis[0] + uteis[1] > alvo:
        return Busca(NENHUM)
    tipos = sorted(Counter(uteis).items())
    if soma_total == alvo:
        return Busca(UNICO, dict(tipos))

    divisor = reduce(gcd, (valor for valor, _ in tipos))
    if alvo % divisor:
        return Busca(NENHUM)
    escala = [(valor // divisor, copias) for valor, copias in tipos]
    alvo_escala, soma_escala = alvo // divisor, soma_total // divisor

    complemento = soma_escala - alvo_escala
    inverter = complemento < alvo_escala
    alvo_busca = complemento if inverter else alvo_escala

    efetivas = [min(copias, alvo_busca // valor) for valor, copias in escala]
    combinacoes = 1
    for copias in sorted(efetivas)[:-1]:  # o tipo de mais cópias sai por divisão (ver `_enumerar`)
        combinacoes *= copias + 1
        if combinacoes > LIMITE_ENUMERACAO:
            break
    if combinacoes <= LIMITE_ENUMERACAO:
        achado = _enumerar(alvo_busca, escala, efetivas)
    else:
        achado = _buscar_denso(alvo_busca, escala, efetivas, limite_trabalho)

    if achado.situacao != UNICO:
        return achado
    por_escala = achado.quantidades or {}
    quantidades = {}
    for (valor, copias), (valor_escala, _) in zip(tipos, escala, strict=True):
        usadas = por_escala.get(valor_escala, 0)
        quantidade = copias - usadas if inverter else usadas
        if quantidade:
            quantidades[valor] = quantidade
    return Busca(UNICO, quantidades, trabalho=achado.trabalho)
