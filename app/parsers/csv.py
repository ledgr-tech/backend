"""Parser de extratos CSV via pandas, com detecção de encoding e delimitador
(issue #9).

Converte o conteúdo bruto de um arquivo CSV (bytes) numa lista de
lançamentos normalizados em memória, no mesmo formato de saída do parser de
OFX (`app.parsers.tipos.LancamentoNormalizado`) — sem tocar o banco. Formato
suportado conforme ADR-001 (02-decisoes/01-formatos-suportados-mvp.md).

Suposições assumidas por este parser, sem ADR fechando o formato exato do
CSV de banco/sistema de gestão (limitação conhecida do MVP, revisitar se
algum banco/sistema real exigir formato diferente):
- Colunas obrigatórias `data`, `valor` e `descricao`, comparadas
  case-insensitive (ex: "Data", "VALOR" batem; acento não é normalizado,
  então uma coluna literalmente "Descrição" não bate com "descricao").
- Data em ISO 8601 (`AAAA-MM-DD`) ou formato brasileiro dia primeiro
  (`DD/MM/AAAA`, `DD-MM-AAAA`).
- Valor com ponto como separador decimal, sem separador de milhar (mesma
  convenção do `TRNAMT` do OFX, ver app/parsers/ofx.py) — CSV com vírgula
  decimal (ex: "1.500,75") não é suportado ainda.
- Tipo (credito/debito) inferido pelo sinal do valor, igual o fallback que
  já existe no parser de OFX — CSV não tem um campo TRNTYPE equivalente.
- Encoding restrito a UTF-8, ISO-8859-1 (Latin-1) ou CP1252: os três
  praticamente usados por banco/sistema de gestão brasileiro. Restringir o
  universo de candidatos do charset-normalizer é intencional (não só
  otimização) — sem essa restrição, texto curto em português é
  frequentemente confundido com outro encoding de acentuação parecida (ex:
  CP1250, da Europa Central).

Por decisão confirmada com o time (mesma da issue #8), esta issue cobre só
a função de parsing. Persistir lançamento (model `Lancamento` + migration)
nasce junto com a normalização via BackgroundTasks (Sprint 2, ver
07-tecnico/backlog-de-sprints-do-mvp.md), evitando retrabalho.
"""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
from charset_normalizer import from_bytes
from dateutil.parser import ParserError
from dateutil.parser import parse as dateutil_parse

from app.parsers.tipos import LancamentoNormalizado

# Ver suposição de encoding no docstring do módulo.
_ENCODINGS_CANDIDATOS = ["utf_8", "iso-8859-1", "cp1252"]
_COLUNAS_OBRIGATORIAS = {"data", "valor", "descricao"}


class CSVInvalidoError(Exception):
    """Conteúdo informado não é um CSV de extrato válido.

    Levantado por encoding não detectável, delimitador não identificável,
    colunas obrigatórias ausentes, ou linha com data/valor não parseável.
    Usado pela issue #11 (teste de arquivo corrompido).
    """


def _detectar_texto(conteudo: bytes) -> str:
    resultado = from_bytes(bytes(conteudo), cp_isolation=_ENCODINGS_CANDIDATOS).best()
    if resultado is None:
        raise CSVInvalidoError(
            "Não foi possível detectar o encoding do arquivo CSV "
            f"(candidatos tentados: {_ENCODINGS_CANDIDATOS})."
        )
    return str(resultado)


def _detectar_delimitador(texto: str) -> str:
    amostra = "\n".join(texto.splitlines()[:10])
    try:
        dialeto = csv.Sniffer().sniff(amostra, delimiters=",;\t|")
    except csv.Error as exc:
        raise CSVInvalidoError(f"Não foi possível detectar o delimitador do CSV: {exc}") from exc
    return dialeto.delimiter


def _parse_data(bruto: str) -> date:
    texto = bruto.strip()
    try:
        return date.fromisoformat(texto)
    except ValueError:
        pass
    # Não-ISO: tenta formato brasileiro (dia primeiro), ex: "05/09/2026".
    try:
        return dateutil_parse(texto, dayfirst=True).date()
    except (ParserError, ValueError, OverflowError) as exc:
        raise CSVInvalidoError(f"Data inválida no CSV: {bruto!r}") from exc


def _parse_valor(bruto: str) -> Decimal:
    try:
        return Decimal(bruto.strip())
    except InvalidOperation as exc:
        raise CSVInvalidoError(f"Valor inválido no CSV: {bruto!r}") from exc


def parse_csv(conteudo: bytes) -> list[LancamentoNormalizado]:
    """Faz o parsing de um arquivo CSV e retorna os lançamentos normalizados.

    Detecta encoding (charset-normalizer) e delimitador (`csv.Sniffer`)
    automaticamente. Levanta `CSVInvalidoError` se `conteudo` não for um CSV
    de extrato válido (ver suposições documentadas no topo do módulo).
    """
    texto = _detectar_texto(conteudo)
    if not texto.strip():
        raise CSVInvalidoError("Arquivo CSV vazio.")

    delimitador = _detectar_delimitador(texto)

    try:
        tabela = pd.read_csv(io.StringIO(texto), sep=delimitador, dtype=str, keep_default_na=False)
    except pd.errors.ParserError as exc:
        raise CSVInvalidoError(f"Não foi possível fazer o parsing do CSV: {exc}") from exc

    tabela.columns = [str(coluna).strip().lower() for coluna in tabela.columns]
    colunas_faltando = _COLUNAS_OBRIGATORIAS - set(tabela.columns)
    if colunas_faltando:
        raise CSVInvalidoError(
            f"CSV não tem as colunas obrigatórias {sorted(colunas_faltando)}. "
            f"Colunas encontradas: {list(tabela.columns)}."
        )

    lancamentos: list[LancamentoNormalizado] = []
    for _, linha in tabela.iterrows():
        valor = _parse_valor(linha["valor"])
        lancamentos.append(
            LancamentoNormalizado(
                data=_parse_data(linha["data"]),
                valor=valor,
                descricao=str(linha["descricao"]).strip(),
                tipo="credito" if valor >= 0 else "debito",
            )
        )
    return lancamentos
