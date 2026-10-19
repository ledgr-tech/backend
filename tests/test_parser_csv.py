from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsers.csv import CSVInvalidoError, parse_csv
from app.parsers.tipos import LancamentoNormalizado, SaldoExtrato

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
#
# Cobertura dos critérios da issue #11 ("Testes unitários de parsing")
# neste arquivo:
# - CSV válido: test_parse_csv_valido_utf8_retorna_lancamentos_normalizados
# - Encoding diferente: test_parse_csv_com_encoding_diferente_retorna_lancamentos_normalizados
# - Arquivo corrompido (erro estrutural, continua abortando o arquivo
#   inteiro mesmo depois da #13): test_parse_csv_corrompido_levanta_erro_especifico
#   (conteúdo genérico inválido), test_parse_csv_vazio_levanta_erro_especifico
#   (arquivo vazio) e test_parse_csv_sem_colunas_obrigatorias_levanta_erro_especifico
#   (estrutura fora do esperado)
# Reforço com corrupção "realista" (gerada pelo script da #10, não bytes de
# lixo hardcoded): ver tests/test_parsers_gerados.py.
#
# Cobertura da tolerância por linha da issue #13:
# test_parse_csv_linha_invalida_no_meio_tolera_e_segue.


def test_parse_csv_valido_utf8_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido.csv").read_bytes()

    resultado = parse_csv(conteudo)

    assert resultado.erros == []
    assert resultado.lancamentos == _LANCAMENTOS_ESPERADOS


def test_parse_csv_com_encoding_diferente_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido_latin1.csv").read_bytes()

    resultado = parse_csv(conteudo)

    assert resultado.erros == []
    assert resultado.lancamentos == _LANCAMENTOS_ESPERADOS


def test_parse_csv_corrompido_levanta_erro_especifico():
    with pytest.raises(CSVInvalidoError):
        parse_csv(b"isso claramente nao eh um csv de extrato valido")


def test_parse_csv_vazio_levanta_erro_especifico():
    with pytest.raises(CSVInvalidoError):
        parse_csv(b"")


def test_parse_csv_sem_colunas_obrigatorias_levanta_erro_especifico():
    conteudo = b"coluna_a,coluna_b\n1,2\n"
    with pytest.raises(CSVInvalidoError):
        parse_csv(conteudo)


def test_parse_csv_linha_invalida_no_meio_tolera_e_segue():
    conteudo = (
        b"data,valor,descricao\n"
        b"2026-09-05,1500.00,Deposito ok\n"
        b"2026-09-06,NAO_E_NUMERO,Linha quebrada\n"
        b"2026-09-07,300.00,Outro deposito ok\n"
    )

    resultado = parse_csv(conteudo)

    assert [l.descricao for l in resultado.lancamentos] == ["Deposito ok", "Outro deposito ok"]
    assert len(resultado.erros) == 1
    # Linha 1 = cabeçalho, linha 2 = primeira linha de dado (bate com o que
    # se vê abrindo o CSV num editor de texto) — a quebrada é a linha 3.
    assert resultado.erros[0].identificador == "3"
    assert "NAO_E_NUMERO" in resultado.erros[0].motivo


# Saldo do extrato (issue #118): coluna opcional `saldo`, vale a da última
# linha do dia mais recente na ordem do arquivo.

_SALDO_ESPERADO = SaldoExtrato(valor=Decimal("1120.00"), data=date(2026, 9, 7))


def test_parse_csv_crescente_com_duas_linhas_no_ultimo_dia_usa_a_ultima_do_arquivo():
    conteudo = (
        b"data,valor,descricao,saldo\n"
        b"2026-09-05,100.00,a,1100.00\n"
        b"2026-09-07,50.00,b,1150.00\n"
        b"2026-09-07,-30.00,c,1120.00\n"
    )

    resultado = parse_csv(conteudo)

    assert resultado.saldo == _SALDO_ESPERADO
    assert len(resultado.lancamentos) == 3


def test_parse_csv_decrescente_com_duas_linhas_no_ultimo_dia_usa_a_primeira_do_arquivo():
    conteudo = (
        b"data,valor,descricao,saldo\n"
        b"2026-09-07,-30.00,c,1120.00\n"
        b"2026-09-07,50.00,b,1150.00\n"
        b"2026-09-05,100.00,a,1100.00\n"
    )

    assert parse_csv(conteudo).saldo == _SALDO_ESPERADO


def test_parse_csv_coluna_saldo_aceita_maiuscula_espaco_e_outro_delimitador():
    conteudo = (
        b"Data;Valor;Descricao; SALDO \n"
        b"2026-09-05;100.00;a;1100.00\n"
        b"07/09/2026;20.00;b;1120.00\n"
    )

    assert parse_csv(conteudo).saldo == _SALDO_ESPERADO


def test_parse_csv_ordem_sai_das_linhas_validas():
    # A última linha não lê (valor inválido) e é a de data mais antiga; a
    # ordem do arquivo continua crescente pela primeira e última válidas.
    conteudo = (
        b"data,valor,descricao,saldo\n"
        b"2026-09-05,100.00,a,1100.00\n"
        b"2026-09-07,-30.00,b,1050.00\n"
        b"2026-09-07,50.00,c,1120.00\n"
        b"2026-09-01,xx,d,9999.00\n"
    )

    resultado = parse_csv(conteudo)

    assert resultado.saldo == _SALDO_ESPERADO
    assert [erro.identificador for erro in resultado.erros] == ["5"]


def test_parse_csv_sem_coluna_saldo_fica_none():
    resultado = parse_csv((FIXTURES_DIR / "extrato_valido.csv").read_bytes())

    assert resultado.saldo is None
    assert resultado.erros == []


@pytest.mark.parametrize("saldo_da_ultima", [b"", b"abc", b'"1.120,00"'], ids=str)
def test_parse_csv_ultima_linha_sem_saldo_legivel_fica_none_sem_procurar_outra(saldo_da_ultima):
    conteudo = (
        b"data,valor,descricao,saldo\n"
        b"2026-09-05,100.00,a,1100.00\n"
        b"2026-09-07,20.00,b," + saldo_da_ultima + b"\n"
    )

    resultado = parse_csv(conteudo)

    assert resultado.saldo is None
    assert resultado.erros == []
    assert len(resultado.lancamentos) == 2
