"""Os marcadores do assistente depois do ajuste das regex (CodeQL
py/polynomial-redos em `marcadores.py`): a saída não mudou, e nenhuma entrada
hostil leva mais que uma fração de segundo.

Os esperados de `CASOS` foram gravados com o código ANTES do ajuste (frases dos
testes de `test_assistente_marcadores.py` e casos de `R$` com sinal, conta e
agência, datas e competências).
"""

import time
from decimal import Decimal

import pytest

from app.api.assistente import MAXIMO_TURNOS, TAMANHO_MAXIMO_PERGUNTA, TAMANHO_MAXIMO_TURNO
from app.services.ajuda.marcadores import (
    _SEQUENCIA_COM_HIFEN_OU_ESPACO,
    _VALOR,
    DataMarcada,
    marcar_conversa,
)
from app.services.ia.mascaramento import (
    mascarar_identificadores,
    mascarar_numeros_longos,
    mascarar_texto,
)

NOMES = ("Alfa Comércio Ltda", "Alfa")

# (pergunta, histórico, (pergunta marcada, turnos marcados, valores, datas))
CASOS = [
    (
        "o pix de R$ 3.200 bateu?",
        (),
        ("o pix de [VALOR_1] bateu?", (), {"[VALOR_1]": Decimal(3200)}, {}),
    ),
    (
        "o pix de R$ 3.200,00 bateu?",
        (),
        ("o pix de [VALOR_1] bateu?", (), {"[VALOR_1]": Decimal("3200.00")}, {}),
    ),
    (
        "o pix de R$3200,5 bateu?",
        (),
        ("o pix de [VALOR_1] bateu?", (), {"[VALOR_1]": Decimal("3200.5")}, {}),
    ),
    ("e o de 123.456,78?", (), ("e o de [VALOR_1]?", (), {"[VALOR_1]": Decimal("123456.78")}, {})),
    (
        "e o de 1.234.567,89?",
        (),
        ("e o de [VALOR_1]?", (), {"[VALOR_1]": Decimal("1234567.89")}, {}),
    ),
    ("e o de 1234,56?", (), ("e o de [VALOR_1]?", (), {"[VALOR_1]": Decimal("1234.56")}, {})),
    ("e o de 1234.56?", (), ("e o de [VALOR_1]?", (), {"[VALOR_1]": Decimal("1234.56")}, {})),
    (
        "e o débito de -150,00?",
        (),
        ("e o débito de [VALOR_1]?", (), {"[VALOR_1]": Decimal("-150.00")}, {}),
    ),
    (
        "o pix de 3200 bateu?",
        (),
        ("o pix de [VALOR_1] bateu?", (), {"[VALOR_1]": Decimal(3200)}, {}),
    ),
    (
        "o pix de 2050 bateu?",
        (),
        ("o pix de [VALOR_1] bateu?", (), {"[VALOR_1]": Decimal(2050)}, {}),
    ),
    (
        "o pix do dia 12/10 bateu?",
        (),
        ("o pix do dia [DATA_1] bateu?", (), {}, {"[DATA_1]": DataMarcada(12, 10, None)}),
    ),
    (
        "pago em 12/10/2026?",
        (),
        ("pago em [DATA_1]?", (), {}, {"[DATA_1]": DataMarcada(12, 10, 2026)}),
    ),
    (
        "pago em 12/10/26?",
        (),
        ("pago em [DATA_1]?", (), {}, {"[DATA_1]": DataMarcada(12, 10, 2026)}),
    ),
    (
        "pago em 2026-10-12?",
        (),
        ("pago em [DATA_1]?", (), {}, {"[DATA_1]": DataMarcada(12, 10, 2026)}),
    ),
    (
        "o pix do dia 12 bateu?",
        (),
        ("o pix do dia [DATA_1] bateu?", (), {}, {"[DATA_1]": DataMarcada(12, None, None)}),
    ),
    (
        "pago em 3 de outubro?",
        (),
        ("pago em [DATA_1]?", (), {}, {"[DATA_1]": DataMarcada(3, 10, None)}),
    ),
    ("como ficou setembro?", (), ("como ficou setembro?", (), {}, {})),
    ("e setembro de 2026?", (), ("e setembro de 2026?", (), {}, {})),
    ("e set/2026?", (), ("e set/2026?", (), {}, {})),
    ("quanto ficou em aberto em 08/2026?", (), ("quanto ficou em aberto em 08/2026?", (), {}, {})),
    ("e em 2026-08?", (), ("e em 2026-08?", (), {}, {})),
    ("e em 2026?", (), ("e em 2026?", (), {}, {})),
    ("e nos últimos 3 meses?", (), ("e nos últimos 3 meses?", (), {}, {})),
    (
        "o CPF 123.456.789-09, o CNPJ 11.222.333/0001-81, o e-mail ana@exemplo.com e o telefone (11) 98765-4321 pagaram R$ 50,00?",
        (),
        (
            "o CPF [CPF], o CNPJ [CNPJ], o e-mail [EMAIL] e o telefone [TELEFONE] pagaram [VALOR_1]?",
            (),
            {"[VALOR_1]": Decimal("50.00")},
            {},
        ),
    ),
    (
        "a conta 12345-6 agência 0001",
        (),
        ("a conta [NUMERO] agência [VALOR_1]", (), {"[VALOR_1]": Decimal(1)}, {}),
    ),
    (
        "R$ 10,00 e R$ 20,00 e de novo 10,00 no dia 12/10 e 13/10",
        (),
        (
            "[VALOR_1] e [VALOR_2] e de novo [VALOR_1] no dia [DATA_1] e [DATA_2]",
            (),
            {"[VALOR_1]": Decimal("10.00"), "[VALOR_2]": Decimal("20.00")},
            {"[DATA_1]": DataMarcada(12, 10, None), "[DATA_2]": DataMarcada(13, 10, None)},
        ),
    ),
    (
        "e no dia 15?",
        (("usuario", "o pix de R$ 3.200 do dia 12 bateu?"), ("assistente", "Achei 1.")),
        (
            "e no dia [DATA_2]?",
            ("o pix de [VALOR_1] do dia [DATA_1] bateu?", "Achei [N]."),
            {"[VALOR_1]": Decimal(3200)},
            {"[DATA_1]": DataMarcada(12, None, None), "[DATA_2]": DataMarcada(15, None, None)},
        ),
    ),
    (
        "e outubro?",
        (
            ("usuario", "como ficou setembro?"),
            (
                "assistente",
                "Setembro/2026: 45 linhas, 34 conciliadas. Saldo final R$ 12.345,67 em 30/09/2026.",
            ),
        ),
        (
            "e outubro?",
            (
                "como ficou setembro?",
                "Setembro/[N]: [N] linhas, [N] conciliadas. Saldo final R$ [N] em [N].",
            ),
            {},
            {},
        ),
    ),
    ("Como envio um extrato?", (), ("Como envio um extrato?", (), {}, {})),
    (
        "saiu R$ - 1.200,00 da conta?",
        (),
        ("saiu [VALOR_1] da conta?", (), {"[VALOR_1]": Decimal("-1200.00")}, {}),
    ),
    (
        "saiu R$ -1.200,00 da conta?",
        (),
        ("saiu [VALOR_1] da conta?", (), {"[VALOR_1]": Decimal("-1200.00")}, {}),
    ),
    (
        "saiu R$-1.200,00 da conta?",
        (),
        ("saiu [VALOR_1] da conta?", (), {"[VALOR_1]": Decimal("-1200.00")}, {}),
    ),
    (
        "saiu R$   -   1.200,00 da conta?",
        (),
        ("saiu [VALOR_1] da conta?", (), {"[VALOR_1]": Decimal("-1200.00")}, {}),
    ),
    (
        "saiu R$ 1.200,00 e R$ -1.200,00 e R$ - 1.200,00",
        (),
        (
            "saiu [VALOR_1] e [VALOR_2] e [VALOR_2]",
            (),
            {"[VALOR_1]": Decimal("1200.00"), "[VALOR_2]": Decimal("-1200.00")},
            {},
        ),
    ),
    (
        "R$ 0,5 e R$ 12 e R$ 1.234.567",
        (),
        (
            "[VALOR_1] e [VALOR_2] e [VALOR_3]",
            (),
            {
                "[VALOR_1]": Decimal("0.5"),
                "[VALOR_2]": Decimal(12),
                "[VALOR_3]": Decimal(1234567),
            },
            {},
        ),
    ),
    ("só R$ e nada", (), ("só R$ e nada", (), {}, {})),
    ("R$ abc 10,00", (), ("R$ abc [VALOR_1]", (), {"[VALOR_1]": Decimal("10.00")}, {})),
    ("R$ - abc", (), ("R$ - abc", (), {}, {})),
    ("R$\t-\t99,90", (), ("R$\t-\t99,90", (), {}, {})),
    ("conta 12345-6", (), ("conta [NUMERO]", (), {}, {})),
    (
        "agência 1234 conta 12345-6",
        (),
        ("agência [VALOR_1] conta [NUMERO]", (), {"[VALOR_1]": Decimal(1234)}, {}),
    ),
    (
        "agência 0001 conta 123456-7 saldo 10,00",
        (),
        (
            "agência [VALOR_1] conta [NUMERO] saldo [VALOR_2]",
            (),
            {"[VALOR_1]": Decimal(1), "[VALOR_2]": Decimal("10.00")},
            {},
        ),
    ),
    ("agência 1234 5678 conta 98765 4", (), ("agência [NUMERO] conta [NUMERO]", (), {}, {})),
    ("1234 5678 9012", (), ("[NUMERO]", (), {}, {})),
    ("1-2-3-4-5-6-7", (), ("[NUMERO]", (), {}, {})),
    ("1 2 3", (), ("1 2 3", (), {}, {})),
    ("12 34", (), ("12 34", (), {}, {})),
    ("ref 12-34 e 56 78 e 9", (), ("ref 12-34 e 56 78 e 9", (), {}, {})),
    ("tel 3456-7890 e 91234-5678", (), ("tel [TELEFONE] e [TELEFONE]", (), {}, {})),
    (
        "duas sequências 11 22 33 e 44-55-66 juntas",
        (),
        ("duas sequências [NUMERO] e [NUMERO] juntas", (), {}, {}),
    ),
    ("termina com separador 123 456 ", (), ("termina com separador [NUMERO] ", (), {}, {})),
    ("termina com hífen 123-456-", (), ("termina com hífen [NUMERO]-", (), {}, {})),
    (
        "de 01/01/2026 a 31/12/2026",
        (),
        (
            "de [DATA_1] a [DATA_2]",
            (),
            {},
            {"[DATA_1]": DataMarcada(1, 1, 2026), "[DATA_2]": DataMarcada(31, 12, 2026)},
        ),
    ),
    (
        "dia 31/02 e dia 32 e 13/13",
        (),
        ("dia [DATA_1] e dia 32 e 13/13", (), {}, {"[DATA_1]": DataMarcada(31, 2, None)}),
    ),
    (
        "em 5 de março e 10 de dez",
        (),
        (
            "em [DATA_1] e [DATA_2]",
            (),
            {},
            {"[DATA_1]": DataMarcada(5, 3, None), "[DATA_2]": DataMarcada(10, 12, None)},
        ),
    ),
    (
        "competência 2026-08 e 08/2026 e agosto de 2026 e ago/2026",
        (),
        ("competência 2026-08 e 08/2026 e agosto de 2026 e ago/2026", (), {}, {}),
    ),
    ("ano 2026 desde 2025 até 2027", (), ("ano 2026 desde 2025 até 2027", (), {}, {})),
    (
        "2026-10-12 12/10/2026 12/10/26 12/10",
        (),
        (
            "[DATA_1] [DATA_1] [DATA_1] [DATA_2]",
            (),
            {},
            {"[DATA_1]": DataMarcada(12, 10, 2026), "[DATA_2]": DataMarcada(12, 10, None)},
        ),
    ),
    (
        "o pix de R$ 3.200,00 do dia 12/10 na conta 12345-6 agência 1234",
        (),
        (
            "o pix de [VALOR_1] do dia [DATA_1] na conta [NUMERO] agência [VALOR_2]",
            (),
            {"[VALOR_1]": Decimal("3200.00"), "[VALOR_2]": Decimal(1234)},
            {"[DATA_1]": DataMarcada(12, 10, None)},
        ),
    ),
    (
        "pix 1234 5678 de R$ 99,90 em 3 de outubro",
        (),
        (
            "pix [NUMERO] de [VALOR_1] em [DATA_1]",
            (),
            {"[VALOR_1]": Decimal("99.90")},
            {"[DATA_1]": DataMarcada(3, 10, None)},
        ),
    ),
    (
        "e a empresa Alfa Comércio Ltda pagou R$ 10,00?",
        (),
        ("e a empresa [EMPRESA] pagou [VALOR_1]?", (), {"[VALOR_1]": Decimal("10.00")}, {}),
    ),
]


@pytest.mark.parametrize(("pergunta", "historico", "esperado"), CASOS)
def test_a_saida_dos_marcadores_nao_mudou(pergunta, historico, esperado):
    marcada = marcar_conversa(pergunta, historico, NOMES)

    assert (
        marcada.pergunta,
        tuple(turno.texto for turno in marcada.historico),
        marcada.valores,
        marcada.datas,
    ) == esperado


# Cada chamada tem de terminar em muito menos que isto. Com o custo quadrático, 10 mil
# caracteres levavam de 0,5 a 1 s; hoje levam milissegundos. A folga é para a CI.
LIMITE_SEGUNDOS = 0.05


def _hostis(tamanho: int) -> dict[str, str]:
    return {
        "zeros": "0" * tamanho,
        "zeros e um x": "0" * (tamanho - 1) + "x",
        "R$ e espacos": ("R$" + " " * tamanho)[:tamanho],
        "R$ - e espacos": ("R$ -" + " " * tamanho)[:tamanho],
        "zero e espaco": ("0 " * tamanho)[:tamanho],
        "um e hifen": ("1-" * tamanho)[:tamanho],
        "R$0 repetido": ("R$0" * tamanho)[:tamanho],
    }


def _melhor_tempo(funcao, *argumentos) -> float:
    """O melhor de três: um pico da CI (coleta de lixo, outro processo) não
    derruba o teste, e o custo quadrático é lento nas três."""
    tempos = []
    for _ in range(3):
        inicio = time.perf_counter()
        funcao(*argumentos)
        tempos.append(time.perf_counter() - inicio)
    return min(tempos)


@pytest.mark.parametrize("nome", list(_hostis(10)))
def test_conversa_no_tamanho_maximo_da_api_e_rapida(nome):
    """O pedido mais pesado que a API aceita: pergunta e todos os turnos do
    histórico, tudo da pessoa, no tamanho máximo."""
    pergunta = _hostis(TAMANHO_MAXIMO_PERGUNTA)[nome]
    turno = _hostis(TAMANHO_MAXIMO_TURNO)[nome]
    historico = [("usuario", turno)] * MAXIMO_TURNOS

    assert _melhor_tempo(marcar_conversa, pergunta, historico) < LIMITE_SEGUNDOS


FUNCOES_DIRETAS = {
    "marcar_conversa": lambda texto: marcar_conversa(texto, ()),
    "sequencia_com_hifen_ou_espaco": lambda texto: _SEQUENCIA_COM_HIFEN_OU_ESPACO.sub("#", texto),
    "valor": lambda texto: _VALOR.sub("#", texto),
    "mascarar_identificadores": mascarar_identificadores,
    "mascarar_numeros_longos": mascarar_numeros_longos,
    "mascarar_texto": mascarar_texto,
}


@pytest.mark.parametrize("funcao", list(FUNCOES_DIRETAS))
@pytest.mark.parametrize("nome", list(_hostis(10)))
def test_10_mil_caracteres_chamando_a_funcao_direto_e_rapido(funcao, nome):
    texto = _hostis(10_000)[nome]

    assert _melhor_tempo(FUNCOES_DIRETAS[funcao], texto) < LIMITE_SEGUNDOS
