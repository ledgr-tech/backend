"""Tipos compartilhados entre os parsers de extrato (issues #8/#9/#13).

`LancamentoNormalizado` é o formato de saída comum — todo parser (OFX, CSV,
e futuros) devolve exatamente essa estrutura, independente do formato de
origem, pra normalização (app/services/normalizacao.py) não precisar
conhecer cada formato.

`ErroLinha`/`ResultadoParsing` (issue #13) são o formato de saída
tolerante: uma linha/transação que não normaliza não aborta o arquivo
inteiro mais (só erro estrutural do arquivo continua abortando, via
OFXInvalidoError/CSVInvalidoError). São dataclasses em memória — não
confundir com o model de banco `app.models.linha_invalida.LinhaInvalida`,
que é o registro persistido a partir de um `ErroLinha` (ver
app/services/normalizacao.py).

`SaldoExtrato` (issue #118) é o saldo que o próprio arquivo informa e a data
dele (LEDGERBAL no OFX, coluna `saldo` no CSV). É opcional: `None` quando o
arquivo não traz, ou traz ilegível, e nunca vira erro do arquivo nem
`ErroLinha`.
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


@dataclass(frozen=True)
class ErroLinha:
    """Uma linha/transação do arquivo que não foi possível normalizar."""

    identificador: str
    motivo: str


# Maior valor absoluto que cabe em NUMERIC(14, 2), a coluna `extratos.saldo_final`.
_SALDO_LIMITE = Decimal("1e12")


@dataclass(frozen=True)
class SaldoExtrato:
    """Saldo informado pelo arquivo do extrato e a data a que ele se refere."""

    valor: Decimal
    data: date


def saldo_ou_none(valor: Decimal, data: date) -> SaldoExtrato | None:
    """`SaldoExtrato`, ou `None` se o valor não couber na coluna (NaN,
    infinito ou grande demais): saldo ilegível fica nulo, não derruba o arquivo."""
    if not valor.is_finite() or abs(valor) >= _SALDO_LIMITE:
        return None
    return SaldoExtrato(valor=valor, data=data)


@dataclass(frozen=True)
class ResultadoParsing:
    """Saída de `parse_ofx`/`parse_csv`: lançamentos válidos + erros por linha,
    e o saldo do arquivo quando ele traz um legível (issue #118)."""

    lancamentos: list[LancamentoNormalizado]
    erros: list[ErroLinha]
    saldo: SaldoExtrato | None = None
