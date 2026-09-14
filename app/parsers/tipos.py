"""Tipos compartilhados entre os parsers de extrato (issues #8/#9).

`LancamentoNormalizado` é o formato de saída comum — todo parser (OFX, CSV,
e futuros) devolve exatamente essa estrutura, independente do formato de
origem, pra normalização (Sprint 2) não precisar conhecer cada formato.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class LancamentoNormalizado:
    """Lançamento em memória, já no schema comum rascunhado na arquitetura técnica."""

    data: date
    valor: Decimal
    descricao: str
    tipo: str  # "credito" | "debito"
