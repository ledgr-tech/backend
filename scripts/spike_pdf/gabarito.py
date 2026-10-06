"""Gabarito de uma amostra, ao lado do PDF:

- `<nome>.gabarito.csv`: UTF-8, separador `;`, colunas `data` (AAAA-MM-DD),
  `descricao` e `valor` (decimal com ponto, negativo para saída).
- `<nome>.gabarito.json`: `{"total": N}`, para ERP que não exporta planilha.
  Só dá para comparar quantidades: medição fraca.
"""

import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.spike_pdf.modelo import Lancamento

COLUNAS = ("data", "descricao", "valor")


@dataclass(frozen=True)
class Gabarito:
    caminho: Path
    lancamentos: list[Lancamento] | None  # None no gabarito só com total
    total: int

    @property
    def so_total(self) -> bool:
        return self.lancamentos is None


def caminho_do_gabarito(pdf: Path) -> Path | None:
    for sufixo in (".gabarito.csv", ".gabarito.json"):
        candidato = pdf.with_name(pdf.stem + sufixo)
        if candidato.exists():
            return candidato
    return None


def ler_gabarito(caminho: Path) -> Gabarito:
    if caminho.name.endswith(".gabarito.json"):
        total = int(json.loads(caminho.read_text(encoding="utf-8"))["total"])
        return Gabarito(caminho=caminho, lancamentos=None, total=total)
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        leitor = csv.DictReader(arquivo, delimiter=";")
        faltando = set(COLUNAS) - set(leitor.fieldnames or ())
        if faltando:
            raise ValueError(f"{caminho.name}: faltam as colunas {sorted(faltando)}")
        lancamentos = [
            Lancamento(
                data=date.fromisoformat(linha["data"].strip()),
                descricao=linha["descricao"].strip(),
                valor=Decimal(linha["valor"].strip()),
            )
            for linha in leitor
        ]
    return Gabarito(caminho=caminho, lancamentos=lancamentos, total=len(lancamentos))


def escrever_gabarito(caminho: Path, lancamentos: list[Lancamento]) -> None:
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        escritor.writerow(COLUNAS)
        for lancamento in lancamentos:
            escritor.writerow(
                [lancamento.data.isoformat(), lancamento.descricao, f"{lancamento.valor:.2f}"]
            )
