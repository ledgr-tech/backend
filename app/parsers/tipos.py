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


@dataclass(frozen=True)
class ResultadoParsing:
    """Saída de `parse_ofx`/`parse_csv`: lançamentos válidos + erros por linha."""

    lancamentos: list[LancamentoNormalizado]
    erros: list[ErroLinha]
