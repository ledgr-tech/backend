"""Parsing de valor e data no formato brasileiro, como aparecem em relatório
de ERP. Devolve `None` quando o texto não é um valor ou data."""

import re
from datetime import date
from decimal import Decimal

# 1.234,56 | 1234,56 | -1.234,56 | (1.234,56) | 1.234,56 D | 1.234,56C | 1.234,56-
_VALOR = re.compile(
    r"""^\s*
    (?P<abre>\()?
    (?P<sinal>[-+])?\s*
    (?:R\$\s*)?
    (?P<numero>\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})
    \s*(?P<sinal_final>-)?
    (?P<fecha>\))?
    \s*(?P<dc>[CDcd])?
    \s*$""",
    re.VERBOSE,
)
_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{2}|\d{4})$")


def parece_valor(texto: str) -> bool:
    return _VALOR.match(texto) is not None


def parse_valor_br(texto: str) -> Decimal | None:
    """`1.234,56` vira 1234.56. Negativo com `-` na frente ou atrás, entre
    parênteses ou com sufixo `D`; sufixo `C` é positivo."""
    casamento = _VALOR.match(texto)
    if casamento is None:
        return None
    if bool(casamento["abre"]) != bool(casamento["fecha"]):
        return None
    valor = Decimal(casamento["numero"].replace(".", "").replace(",", "."))
    negativo = (
        casamento["sinal"] == "-"
        or casamento["sinal_final"] == "-"
        or casamento["abre"] is not None
        or (casamento["dc"] or "").upper() == "D"
    )
    return -valor if negativo else valor


def parse_data_br(texto: str) -> date | None:
    """DD/MM/AAAA ou DD/MM/AA (ano de dois dígitos fica em 20AA)."""
    casamento = _DATA.match(texto.strip())
    if casamento is None:
        return None
    dia, mes, ano = casamento.groups()
    ano_completo = int(ano) + 2000 if len(ano) == 2 else int(ano)
    try:
        return date(ano_completo, int(mes), int(dia))
    except ValueError:
        return None
