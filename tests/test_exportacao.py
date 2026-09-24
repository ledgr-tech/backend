"""Testes unitários (issue #26) da lógica pura de app/services/exportacao.py
— sem banco, sem HTTP. Os testes do endpoint `GET
/conciliacoes/{extrato_id}/exportar` ficam em
tests/test_conciliacoes_exportacao.py.
"""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from app.services.exportacao import (
    LinhaExportacao,
    formatar_data,
    formatar_score,
    formatar_valor,
    gerar_csv_conciliacoes,
    neutralizar_formula,
)

# neutralizar_formula


@pytest.mark.parametrize("gatilho", ["=", "+", "-", "@", "\t", "\r"])
def test_neutralizar_formula_prefixa_cada_caractere_gatilho(gatilho):
    texto = f'{gatilho}HYPERLINK("http://evil")'
    assert neutralizar_formula(texto) == "'" + texto


def test_neutralizar_formula_gatilho_depois_de_espaco_tambem_e_prefixado():
    texto = "  =SOMA(A1:A2)"
    assert neutralizar_formula(texto) == "'" + texto


def test_neutralizar_formula_texto_normal_fica_intacto():
    texto = "Pagamento fornecedor A"
    assert neutralizar_formula(texto) == texto


def test_neutralizar_formula_string_vazia_fica_vazia():
    assert neutralizar_formula("") == ""


# formatar_valor


def test_formatar_valor_negativo():
    assert formatar_valor(Decimal("-150.00")) == "-150,00"


def test_formatar_valor_positivo():
    assert formatar_valor(Decimal("1500.00")) == "1500,00"


def test_formatar_valor_com_centavos():
    assert formatar_valor(Decimal("10.05")) == "10,05"


def test_formatar_valor_none_e_vazio():
    assert formatar_valor(None) == ""


# formatar_data


def test_formatar_data():
    assert formatar_data(date(2026, 9, 5)) == "05/09/2026"


def test_formatar_data_none_e_vazio():
    assert formatar_data(None) == ""


# formatar_score


def test_formatar_score():
    assert formatar_score(Decimal("1.000")) == "1,000"
    assert formatar_score(Decimal("0.500")) == "0,500"


def test_formatar_score_none_e_vazio():
    assert formatar_score(None) == ""


# gerar_csv_conciliacoes


def _decodificar_sem_bom(conteudo: bytes) -> str:
    assert conteudo[:3] == b"\xef\xbb\xbf"
    return conteudo[3:].decode("utf-8")


def test_gerar_csv_tem_bom_utf8_no_inicio():
    conteudo = gerar_csv_conciliacoes([])
    assert conteudo[:3] == b"\xef\xbb\xbf"


def test_gerar_csv_round_trip_de_descricao_com_ponto_e_virgula_aspas_e_quebra_de_linha():
    descricao = 'Pagamento; "fornecedor" A\nsegunda linha'
    linha = LinhaExportacao(
        status="match_exato",
        regra_aplicada="exato",
        score_confianca=Decimal("1.000"),
        banco_data=date(2026, 9, 5),
        banco_valor=Decimal("10.00"),
        banco_descricao=descricao,
        sistema_data=date(2026, 9, 5),
        sistema_valor=Decimal("10.00"),
        sistema_descricao="b",
    )

    texto = _decodificar_sem_bom(gerar_csv_conciliacoes([linha]))
    leitor = csv.reader(io.StringIO(texto), delimiter=";")
    _cabecalho, corpo = list(leitor)

    assert corpo[3] == descricao  # coluna "Descrição (banco)"
