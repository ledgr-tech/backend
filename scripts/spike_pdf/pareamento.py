"""Pareamento entre o extraído e o gabarito, e as métricas de uma amostra.

Uma linha extraída está CORRETA quando tem a mesma data, o mesmo valor exato
(com sinal) e descrição parecida (similaridade das descrições normalizadas de
pelo menos `limiar`, 0,8 por padrão, com `rapidfuzz.fuzz.ratio`). Cada linha
do gabarito casa com no máximo uma extraída, e vice-versa.

Depois das corretas, as sobras com mesma data e descrição parecida mas valor
diferente são ERRO DE VALOR: `sinal_invertido` quando o valor é o do gabarito
com o sinal trocado, `valor_errado` nos outros casos. É o pior caso para a
conciliação (um lançamento entra com valor errado sem ninguém perceber), por
isso aparece separado de "faltando" e "sobrando".
"""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from scripts.spike_pdf.modelo import Lancamento

LIMIAR_PADRAO = 0.8

_ESPACOS = re.compile(r"\s+")


def normalizar_descricao(descricao: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", descricao)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return _ESPACOS.sub(" ", sem_acento.casefold()).strip()


def similaridade(a: str, b: str) -> float:
    return fuzz.ratio(normalizar_descricao(a), normalizar_descricao(b)) / 100


@dataclass
class Resultado:
    corretas: list[tuple[Lancamento, Lancamento]] = field(default_factory=list)
    valor_errado: list[tuple[Lancamento, Lancamento]] = field(default_factory=list)
    sinal_invertido: list[tuple[Lancamento, Lancamento]] = field(default_factory=list)
    faltando: list[Lancamento] = field(default_factory=list)  # no gabarito, não extraídas
    sobrando: list[Lancamento] = field(default_factory=list)  # extraídas, fora do gabarito


def _parear(gabarito, extraidos, pode_casar, limiar):
    """Pareamento guloso pelo par de maior similaridade, entre os que passam
    em `pode_casar`. Devolve os pares e os índices usados de cada lado."""
    candidatos = []
    for i, esperado in enumerate(gabarito):
        for j, obtido in enumerate(extraidos):
            if esperado.data != obtido.data or not pode_casar(esperado, obtido):
                continue
            nota = similaridade(esperado.descricao, obtido.descricao)
            if nota >= limiar:
                candidatos.append((-nota, i, j))
    candidatos.sort()
    usados_g, usados_e, pares = set(), set(), []
    for _, i, j in candidatos:
        if i in usados_g or j in usados_e:
            continue
        usados_g.add(i)
        usados_e.add(j)
        pares.append((gabarito[i], extraidos[j]))
    return pares, usados_g, usados_e


def parear(
    gabarito: list[Lancamento], extraidos: list[Lancamento], limiar: float = LIMIAR_PADRAO
) -> Resultado:
    resultado = Resultado()
    corretas, usados_g, usados_e = _parear(
        gabarito, extraidos, lambda a, b: a.valor == b.valor, limiar
    )
    resultado.corretas = corretas

    resto_g = [g for i, g in enumerate(gabarito) if i not in usados_g]
    resto_e = [e for j, e in enumerate(extraidos) if j not in usados_e]
    erros, usados_g2, usados_e2 = _parear(resto_g, resto_e, lambda a, b: True, limiar)
    for esperado, obtido in erros:
        if obtido.valor == -esperado.valor:
            resultado.sinal_invertido.append((esperado, obtido))
        else:
            resultado.valor_errado.append((esperado, obtido))

    resultado.faltando = [g for i, g in enumerate(resto_g) if i not in usados_g2]
    resultado.sobrando = [e for j, e in enumerate(resto_e) if j not in usados_e2]
    return resultado


@dataclass(frozen=True)
class Metricas:
    gabarito: int
    extraidas: int
    corretas: int
    valor_errado: int
    sinal_invertido: int
    faltando: int
    sobrando: int

    @property
    def recall(self) -> float | None:
        return self.corretas / self.gabarito if self.gabarito else None

    @property
    def precisao(self) -> float | None:
        return self.corretas / self.extraidas if self.extraidas else None

    @property
    def erros_de_valor(self) -> int:
        return self.valor_errado + self.sinal_invertido


def metricas(gabarito: list[Lancamento], extraidos: list[Lancamento], resultado: Resultado):
    return Metricas(
        gabarito=len(gabarito),
        extraidas=len(extraidos),
        corretas=len(resultado.corretas),
        valor_errado=len(resultado.valor_errado),
        sinal_invertido=len(resultado.sinal_invertido),
        faltando=len(resultado.faltando),
        sobrando=len(resultado.sobrando),
    )
