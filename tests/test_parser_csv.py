from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsers.csv import CSVInvalidoError, parse_csv
from app.parsers.tipos import LancamentoNormalizado

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_LANCAMENTOS_ESPERADOS = [
    LancamentoNormalizado(
        data=date(2026, 9, 5),
        valor=Decimal("1500.00"),
        descricao="Depósito cliente São Paulo",
        tipo="credito",
    ),
    LancamentoNormalizado(
        data=date(2026, 9, 6),
        valor=Decimal("-250.75"),
        descricao="Pagamento fornecedor múltiplo",
        tipo="debito",
    ),
    LancamentoNormalizado(
        data=date(2026, 9, 7),
        valor=Decimal("300.00"),
        descricao="Transferência recebida",
        tipo="credito",
    ),
]

# Fixtures criadas à mão pra issue #9 (não existe ainda o script gerador de
# extratos sintéticos da issue #10) — mesmo conteúdo em dois encodings, pra
# cobrir os dois cenários do critério de aceite da issue.


def test_parse_csv_valido_utf8_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido.csv").read_bytes()

    lancamentos = parse_csv(conteudo)

    assert lancamentos == _LANCAMENTOS_ESPERADOS


def test_parse_csv_com_encoding_diferente_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido_latin1.csv").read_bytes()

    lancamentos = parse_csv(conteudo)

    assert lancamentos == _LANCAMENTOS_ESPERADOS


def test_parse_csv_invalido_levanta_erro_especifico():
    with pytest.raises(CSVInvalidoError):
        parse_csv(b"isso claramente nao eh um csv de extrato valido")


def test_parse_csv_vazio_levanta_erro_especifico():
    with pytest.raises(CSVInvalidoError):
        parse_csv(b"")


def test_parse_csv_sem_colunas_obrigatorias_levanta_erro_especifico():
    conteudo = b"coluna_a,coluna_b\n1,2\n"
    with pytest.raises(CSVInvalidoError):
        parse_csv(conteudo)
