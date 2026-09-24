"""Testes de `mascarar_descricao` (issue #28, ADR-011) — função pura, sem
banco, sem rede.
"""

import pytest

from app.services.ia.mascaramento import mascarar_descricao

# cada tipo, na ordem da docstring do módulo


def test_mascara_cnpj_pontuado():
    assert mascarar_descricao("CNPJ 12.345.678/0001-95 pagamento") == "CNPJ [CNPJ] pagamento"


def test_mascara_cnpj_cru():
    assert mascarar_descricao("CNPJ cru 12345678000195 pagamento") == "CNPJ cru [CNPJ] pagamento"


def test_mascara_cpf_pontuado():
    assert mascarar_descricao("CPF 123.456.789-01 titular") == "CPF [CPF] titular"


@pytest.mark.parametrize(
    "texto",
    ["CPF parcial •••.123.456-•• titular", "CPF parcial ***.123.456-** titular"],
    ids=["bullets", "asteriscos"],
)
def test_mascara_cpf_parcialmente_oculto(texto):
    assert mascarar_descricao(texto) == "CPF parcial [CPF] titular"


def test_mascara_email():
    assert (
        mascarar_descricao("contato joao.silva@empresa.com.br obrigado")
        == "contato [EMAIL] obrigado"
    )


@pytest.mark.parametrize(
    "texto",
    [
        "ligar (11) 91234-5678 as 10h",
        "ligar +55 11 91234-5678 as 10h",
        "ligar 11 3456-7890 as 10h",
        "ligar 3456-7890 as 10h",
    ],
)
def test_mascara_telefone(texto):
    resultado = mascarar_descricao(texto)
    assert resultado.endswith(" as 10h")
    assert resultado.startswith("ligar [TELEFONE]")
    assert "[TELEFONE]" in resultado
    assert not any(char.isdigit() for char in resultado.removesuffix(" as 10h"))


def test_mascara_sequencia_longa_de_digitos():
    assert mascarar_descricao("referencia 123456 do pedido") == "referencia [NUMERO] do pedido"


def test_mascara_sequencia_longa_separada_por_ponto():
    assert mascarar_descricao("referencia 1.234.567 do pedido") == "referencia [NUMERO] do pedido"


def test_mascara_sequencia_longa_separada_por_hifen():
    assert mascarar_descricao("codigo 123-456-789 aqui") == "codigo [NUMERO] aqui"


def test_sequencia_curta_fica_intacta():
    assert mascarar_descricao("pedido 1234 confirmado") == "pedido 1234 confirmado"


# valores monetários e datas curtas preservados


def test_valor_monetario_fica_intacto():
    assert mascarar_descricao("R$ 150,00 pago") == "R$ 150,00 pago"


def test_valor_monetario_negativo_fica_intacto():
    assert mascarar_descricao("debito de R$ 1500,00 na conta") == "debito de R$ 1500,00 na conta"


def test_data_curta_fica_intacta():
    assert mascarar_descricao("pagamento em 12/09 confirmado") == "pagamento em 12/09 confirmado"


# truncamento (depois do mascaramento)


def test_truncamento_em_120_caracteres():
    texto = "a" * 200
    resultado = mascarar_descricao(texto)
    assert len(resultado) == 120
    assert resultado == "a" * 120


def test_truncamento_acontece_depois_do_mascaramento():
    # o CNPJ está bem no início; se o truncamento rodasse antes da máscara,
    # cortaria o CNPJ pela metade em vez de substituí-lo por [CNPJ].
    texto = "12.345.678/0001-95 " + "x" * 200
    resultado = mascarar_descricao(texto)
    assert resultado.startswith("[CNPJ] ")
    assert len(resultado) == 120


# idempotência


@pytest.mark.parametrize(
    "texto",
    [
        "CNPJ 12.345.678/0001-95 e email joao@ex.com e fone (11) 91234-5678",
        "referencia 123456 do pedido",
        "texto normal sem nada de especial",
        "",
    ],
)
def test_idempotente(texto):
    uma_vez = mascarar_descricao(texto)
    duas_vezes = mascarar_descricao(uma_vez)
    assert uma_vez == duas_vezes


def test_texto_vazio():
    assert mascarar_descricao("") == ""


def test_texto_normal_sem_dado_sensivel_fica_intacto():
    texto = "Pagamento de aluguel referente ao mes de setembro"
    assert mascarar_descricao(texto) == texto
