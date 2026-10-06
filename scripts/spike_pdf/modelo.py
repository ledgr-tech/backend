"""Lançamento no formato comum do spike: o mesmo para gabarito e extração."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Lancamento:
    data: date
    descricao: str
    valor: Decimal  # negativo para saída
