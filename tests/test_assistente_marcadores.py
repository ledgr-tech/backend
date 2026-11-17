"""Marcadores da conversa com o assistente (issue #223, ADR-028).

Antes de a pergunta sair para o provedor, cada valor e cada data viram um
marcador (`[VALOR_1]`, `[DATA_1]`), além da máscara de identificadores do
ADR-011. O mapa entre marcador e valor fica só no backend. Nos turnos do
assistente que voltam no histórico, todo número vira `[N]`.
"""

import re
from decimal import Decimal

import pytest

from app.services.ajuda.marcadores import DataMarcada, marcar_conversa


def _pergunta(texto):
    return marcar_conversa(texto, ()).pergunta


@pytest.mark.parametrize(
    ("texto", "esperado", "valor"),
    [
        ("o pix de R$ 3.200 bateu?", "o pix de [VALOR_1] bateu?", Decimal(3200)),
        ("o pix de R$ 3.200,00 bateu?", "o pix de [VALOR_1] bateu?", Decimal("3200.00")),
        ("o pix de R$3200,5 bateu?", "o pix de [VALOR_1] bateu?", Decimal("3200.5")),
        ("e o de 123.456,78?", "e o de [VALOR_1]?", Decimal("123456.78")),
        ("e o de 1.234.567,89?", "e o de [VALOR_1]?", Decimal("1234567.89")),
        ("e o de 1234,56?", "e o de [VALOR_1]?", Decimal("1234.56")),
        ("e o de 1234.56?", "e o de [VALOR_1]?", Decimal("1234.56")),
        ("e o débito de -150,00?", "e o débito de [VALOR_1]?", Decimal("-150.00")),
        ("o pix de 3200 bateu?", "o pix de [VALOR_1] bateu?", Decimal(3200)),
        ("o pix de 2050 bateu?", "o pix de [VALOR_1] bateu?", Decimal(2050)),
    ],
)
def test_valores_viram_marcador_e_o_valor_fica_no_mapa(texto, esperado, valor):
    marcada = marcar_conversa(texto, ())

    assert marcada.pergunta == esperado
    assert marcada.valores == {"[VALOR_1]": valor}


@pytest.mark.parametrize(
    ("texto", "esperado", "data"),
    [
        ("o pix do dia 12/10 bateu?", "o pix do dia [DATA_1] bateu?", DataMarcada(12, 10, None)),
        ("pago em 12/10/2026?", "pago em [DATA_1]?", DataMarcada(12, 10, 2026)),
        ("pago em 12/10/26?", "pago em [DATA_1]?", DataMarcada(12, 10, 2026)),
        ("pago em 2026-10-12?", "pago em [DATA_1]?", DataMarcada(12, 10, 2026)),
        ("o pix do dia 12 bateu?", "o pix do dia [DATA_1] bateu?", DataMarcada(12, None, None)),
        ("pago em 3 de outubro?", "pago em [DATA_1]?", DataMarcada(3, 10, None)),
    ],
)
def test_datas_viram_marcador_e_a_data_fica_no_mapa(texto, esperado, data):
    marcada = marcar_conversa(texto, ())

    assert marcada.pergunta == esperado
    assert marcada.datas == {"[DATA_1]": data}


@pytest.mark.parametrize(
    "texto",
    [
        "como ficou setembro?",
        "e setembro de 2026?",
        "e set/2026?",
        "quanto ficou em aberto em 08/2026?",
        "e em 2026-08?",
        "e em 2026?",
        "e nos últimos 3 meses?",
    ],
)
def test_mes_ano_e_numero_pequeno_continuam_no_texto(texto):
    """Mês e ano não são dado sensível, e o modelo precisa deles para achar a
    competência."""
    marcada = marcar_conversa(texto, ())

    assert marcada.pergunta == texto
    assert marcada.valores == {} and marcada.datas == {}


def test_identificadores_continuam_mascarados_e_nao_viram_valor():
    marcada = marcar_conversa(
        "o CPF 123.456.789-09, o CNPJ 11.222.333/0001-81, o e-mail ana@exemplo.com "
        "e o telefone (11) 98765-4321 pagaram R$ 50,00?",
        (),
    )

    assert marcada.pergunta == (
        "o CPF [CPF], o CNPJ [CNPJ], o e-mail [EMAIL] e o telefone [TELEFONE] pagaram [VALOR_1]?"
    )
    assert marcada.valores == {"[VALOR_1]": Decimal("50.00")}


def test_numero_longo_que_sobra_vira_numero_mascarado():
    """Conta bancária e afins: a sequência longa que não é valor nem data
    continua com a máscara do ADR-011."""
    assert _pergunta("a conta 12345-6 agência 0001") == "a conta [NUMERO] agência [VALOR_1]"


def test_o_mesmo_valor_reusa_o_marcador_e_valores_diferentes_ganham_outro():
    marcada = marcar_conversa("R$ 10,00 e R$ 20,00 e de novo 10,00 no dia 12/10 e 13/10", ())

    assert (
        marcada.pergunta == "[VALOR_1] e [VALOR_2] e de novo [VALOR_1] no dia [DATA_1] e [DATA_2]"
    )
    assert marcada.valores == {"[VALOR_1]": Decimal("10.00"), "[VALOR_2]": Decimal("20.00")}


def test_numeracao_e_compartilhada_entre_historico_e_pergunta():
    """O modelo pode citar um valor de uma pergunta anterior ("e no dia 15?")."""
    marcada = marcar_conversa(
        "e no dia 15?",
        (("usuario", "o pix de R$ 3.200 do dia 12 bateu?"), ("assistente", "Achei 1.")),
    )

    assert marcada.historico[0].texto == "o pix de [VALOR_1] do dia [DATA_1] bateu?"
    assert marcada.pergunta == "e no dia [DATA_2]?"
    assert marcada.valores == {"[VALOR_1]": Decimal(3200)}
    assert set(marcada.datas) == {"[DATA_1]", "[DATA_2]"}


def test_turno_do_assistente_perde_todos_os_numeros():
    """Uma resposta de dados anterior volta no histórico: nenhum número dela
    vai ao provedor."""
    marcada = marcar_conversa(
        "e outubro?",
        (
            ("usuario", "como ficou setembro?"),
            (
                "assistente",
                "Setembro/2026: 45 linhas, 34 conciliadas. Saldo final R$ 12.345,67 em 30/09/2026.",
            ),
        ),
    )

    texto = marcada.historico[1].texto
    assert not re.search(r"\d", texto)
    assert texto == "Setembro/[N]: [N] linhas, [N] conciliadas. Saldo final R$ [N] em [N]."
    assert marcada.historico[1].papel == "assistente"
    assert marcada.valores == {} and marcada.datas == {}


def test_texto_sem_numero_passa_igual():
    assert _pergunta("Como envio um extrato?") == "Como envio um extrato?"
