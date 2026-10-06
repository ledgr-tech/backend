"""Converte a exportação em Excel ou CSV do ERP para o formato de gabarito do
spike (`data;descricao;valor`, data AAAA-MM-DD, valor com ponto e negativo
para saída). Simples de propósito: ajuste os parâmetros por ERP.

Colunas por nome do cabeçalho ou por posição (0, 1, 2...). Valor de uma das
três formas:

- `--valor COL`: uma coluna com sinal ("-1.234,56", "(1.234,56)" ou
  "1.234,56 D" também servem);
- `--valor COL --dc COL`: valor sem sinal e uma coluna com C/D;
- `--entrada COL --saida COL`: duas colunas, a de saída vira negativa.

Exemplo:

    python -m scripts.spike_pdf.converter_gabarito --arquivo erp.xlsx \\
        --data "Data" --descricao "Histórico" --entrada "Crédito" --saida "Débito" \\
        --gabarito scripts/spike_pdf/amostras/erp_x.gabarito.csv

Linhas sem data válida (totais, subtotais, linhas em branco) são puladas, e
o terminal só mostra quantas.
"""

import argparse
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from scripts.spike_pdf.gabarito import escrever_gabarito
from scripts.spike_pdf.modelo import Lancamento
from scripts.spike_pdf.valores import parse_valor_br


def _ler(arquivo: Path, separador: str, codificacao: str, pular: int, planilha) -> pd.DataFrame:
    if arquivo.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(arquivo, sheet_name=planilha or 0, skiprows=pular, dtype=object)
    return pd.read_csv(
        arquivo,
        sep=separador,
        encoding=codificacao,
        skiprows=pular,
        dtype=str,
        keep_default_na=False,
    )


def _coluna(tabela: pd.DataFrame, nome: str | None):
    if nome is None:
        return None
    if nome not in tabela.columns and nome.isdigit():
        return tabela.iloc[:, int(nome)]
    return tabela[nome]


def _vazio(valor) -> bool:
    return (
        valor is None or (isinstance(valor, float) and pd.isna(valor)) or str(valor).strip() == ""
    )


def converter_valor(valor) -> Decimal | None:
    if _vazio(valor):
        return None
    if isinstance(valor, int | float):
        return Decimal(str(round(float(valor), 2))).quantize(Decimal("0.01"))
    texto = str(valor).strip()
    convertido = parse_valor_br(texto)
    if convertido is not None:
        return convertido
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def converter_data(valor, formato: str) -> date | None:
    if _vazio(valor):
        return None
    if isinstance(valor, datetime | pd.Timestamp):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        lida = time.strptime(str(valor).strip(), formato)
    except ValueError:
        return None
    return date(lida.tm_year, lida.tm_mon, lida.tm_mday)


def converter(args) -> tuple[list[Lancamento], int]:
    tabela = _ler(args.arquivo, args.separador, args.codificacao, args.pular, args.planilha)
    datas = _coluna(tabela, args.data)
    descricoes = [_coluna(tabela, nome) for nome in args.descricao]
    valores = _coluna(tabela, args.valor)
    entradas = _coluna(tabela, args.entrada)
    saidas = _coluna(tabela, args.saida)
    dcs = _coluna(tabela, args.dc)

    lancamentos, pulados = [], 0
    for indice in range(len(tabela)):
        data = converter_data(datas.iloc[indice], args.formato_data)
        if valores is not None:
            valor = converter_valor(valores.iloc[indice])
            if valor is not None and dcs is not None:
                letra = str(dcs.iloc[indice]).strip().upper()[:1]
                valor = -abs(valor) if letra == "D" else abs(valor) if letra == "C" else valor
        else:
            entrada = converter_valor(entradas.iloc[indice])
            saida = converter_valor(saidas.iloc[indice])
            valor = abs(entrada) if entrada else (-abs(saida) if saida else None)
        descricao = " ".join(
            str(coluna.iloc[indice]).strip()
            for coluna in descricoes
            if not _vazio(coluna.iloc[indice])
        )
        if data is None or valor is None:
            pulados += 1
            continue
        lancamentos.append(Lancamento(data, descricao, valor))
    return lancamentos, pulados


def main() -> None:
    parser = argparse.ArgumentParser(description="Converte a exportação do ERP em gabarito.")
    parser.add_argument("--arquivo", type=Path, required=True, help="Excel ou CSV do ERP")
    parser.add_argument("--gabarito", type=Path, required=True, help="<amostra>.gabarito.csv")
    parser.add_argument("--data", required=True)
    parser.add_argument("--descricao", required=True, action="append", help="repetível")
    parser.add_argument("--valor")
    parser.add_argument("--dc", help="coluna com C/D, junto com --valor")
    parser.add_argument("--entrada")
    parser.add_argument("--saida")
    parser.add_argument("--formato-data", default="%d/%m/%Y")
    parser.add_argument("--separador", default=";")
    parser.add_argument("--codificacao", default="utf-8")
    parser.add_argument("--pular", type=int, default=0, help="linhas antes do cabeçalho")
    parser.add_argument("--planilha", help="nome da aba no Excel")
    args = parser.parse_args()
    if (args.valor is None) == (args.entrada is None and args.saida is None):
        parser.error("informe --valor, ou --entrada e --saida")
    if args.valor is None and (args.entrada is None or args.saida is None):
        parser.error("--entrada e --saida vão juntas")

    lancamentos, pulados = converter(args)
    escrever_gabarito(args.gabarito, lancamentos)
    print(f"{len(lancamentos)} lançamentos gravados em {args.gabarito}; {pulados} linhas puladas")


if __name__ == "__main__":
    main()
