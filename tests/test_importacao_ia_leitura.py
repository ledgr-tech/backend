"""Leitura do JSON da importação via IA (issue #158, ADR-013), sem banco."""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.services.importacao_ia import prompt_atual, schema_atual
from app.services.importacao_ia.leitura import (
    MAXIMO_ERROS,
    ArquivoCortado,
    ArquivoForaDoSchema,
    ArquivoIlegivel,
    ler_importacao,
)

VALIDO = {
    "versao": "1",
    "lancamentos": [
        {"data": "2026-09-05", "valor": "-1234.56", "descricao": "Pagamento fornecedor"},
        {"data": "2026-09-06", "valor": "500", "descricao": "  Recebimento cliente  "},
        {"data": "2026-09-06", "valor": "0.5", "descricao": "Juros"},
    ],
}


def _bytes(documento) -> bytes:
    return json.dumps(documento, ensure_ascii=False).encode("utf-8")


def _erros(documento) -> list[tuple[str, int | None, str]]:
    conteudo = documento if isinstance(documento, bytes) else _bytes(documento)
    with pytest.raises(ArquivoForaDoSchema) as erro:
        ler_importacao(conteudo)
    return [(e.caminho, e.lancamento, e.mensagem) for e in erro.value.erros]


def _com(**campos) -> dict:
    return {"versao": "1", "lancamentos": [{**VALIDO["lancamentos"][0], **campos}]}


# --- válido ---------------------------------------------------------------------------


def test_schema_valido_na_ordem_do_arquivo():
    lancamentos = ler_importacao(_bytes(VALIDO))

    assert [(item.posicao, item.data, item.valor, item.descricao) for item in lancamentos] == [
        (0, date(2026, 9, 5), Decimal("-1234.56"), "Pagamento fornecedor"),
        (1, date(2026, 9, 6), Decimal(500), "Recebimento cliente"),
        (2, date(2026, 9, 6), Decimal("0.5"), "Juros"),
    ]
    assert [item.tipo for item in lancamentos] == ["debito", "credito", "credito"]


@pytest.mark.parametrize(
    "embrulho",
    [
        "```json\n{}\n```",
        "```\n{}\n```",
        "```JSON\n{}\n```\n",
        "  \n```json  \n{}\n  ```  \n\n",
        "﻿{}",
        "\n\n  {}  \n",
    ],
    ids=["json", "sem_lingua", "maiuscula", "espacos", "bom", "puro_com_espacos"],
)
def test_aceita_json_puro_ou_num_bloco_markdown(embrulho):
    conteudo = embrulho.replace("{}", json.dumps(VALIDO)).encode("utf-8")

    assert len(ler_importacao(conteudo)) == 3


# --- texto em volta, vazio e encoding -------------------------------------------------


@pytest.mark.parametrize(
    "conteudo",
    [
        "Aqui está o JSON:\n```json\n{}\n```",
        "```json\n{}\n```\nConfira os valores antes de importar.",
        'Segue: {"versao": "1"}',
        "```python\n{}\n```",
    ],
    ids=["texto_antes", "texto_depois", "texto_solto", "outra_lingua"],
)
def test_texto_fora_do_bloco_e_recusado(conteudo):
    with pytest.raises(ArquivoIlegivel):
        ler_importacao(conteudo.replace("{}", json.dumps(VALIDO)).encode("utf-8"))


@pytest.mark.parametrize("conteudo", [b"", b"   \n\t ", "﻿".encode()])
def test_arquivo_vazio(conteudo):
    with pytest.raises(ArquivoIlegivel) as erro:
        ler_importacao(conteudo)
    assert erro.value.mensagem == "O arquivo está vazio."


def test_fora_de_utf8():
    with pytest.raises(ArquivoIlegivel) as erro:
        ler_importacao(_bytes(VALIDO).replace(b"Pagamento", "Pagamento é".encode("latin-1")))
    assert "UTF-8" in erro.value.mensagem


@pytest.mark.parametrize(
    "conteudo",
    ['{"versao": "1",}', '{"versao": "1", "lancamentos": [1 2]}', "{]", "[}"],
    ids=["virgula_sobrando", "sem_virgula", "fechamento_trocado", "lista_fechada_errado"],
)
def test_json_quebrado_com_tudo_fechado_nao_e_corte(conteudo):
    with pytest.raises(ArquivoIlegivel) as erro:
        ler_importacao(conteudo.encode())
    assert "não é um JSON válido (linha 1" in erro.value.mensagem


# --- arquivo cortado ------------------------------------------------------------------

_INTEIRO = json.dumps(VALIDO, indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    "fim",
    [
        _INTEIRO.index('"lancamentos"') + 5,  # no meio de uma chave
        _INTEIRO.index("-1234") + 3,  # no meio de um valor
        _INTEIRO.index("Pagamento") + 4,  # no meio de uma descrição
        _INTEIRO.index("},") + 1,  # entre dois lançamentos, depois do objeto
        _INTEIRO.index("},") + 2,  # depois da vírgula
        _INTEIRO.rindex("}", 0, len(_INTEIRO) - 1) + 1,  # sem o ] e o } do fim
        len(_INTEIRO) - 1,  # só sem o } do fim
        1,  # só o {
    ],
    ids=[
        "chave",
        "valor",
        "descricao",
        "depois_do_objeto",
        "depois_da_virgula",
        "sem_fechar_a_lista",
        "sem_fechar_o_objeto",
        "so_a_abertura",
    ],
)
@pytest.mark.parametrize("em_bloco", [False, True], ids=["puro", "bloco_fechado"])
def test_json_cortado(fim, em_bloco):
    texto = _INTEIRO[:fim]
    if em_bloco:
        texto = f"```json\n{texto}\n```"

    with pytest.raises(ArquivoCortado):
        ler_importacao(texto.encode("utf-8"))


@pytest.mark.parametrize(
    "texto",
    [f"```json\n{_INTEIRO}", f"```json\n{_INTEIRO}\n``", f"```\n{_INTEIRO[:40]}"],
    ids=["sem_fechamento", "fechamento_incompleto", "cortado_e_sem_fechamento"],
)
def test_bloco_markdown_sem_fechamento_e_corte(texto):
    with pytest.raises(ArquivoCortado):
        ler_importacao(texto.encode("utf-8"))


def test_aspas_escapadas_nao_enganam_o_corte():
    texto = _bytes(_com(descricao='Cheque "nominal" \\ "x"'))

    assert ler_importacao(texto)[0].descricao == 'Cheque "nominal" \\ "x"'
    with pytest.raises(ArquivoCortado):
        ler_importacao(texto[: texto.index(b"nominal") + 9])


# --- raiz e versão --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("documento", "esperado"),
    [
        ([VALIDO], ("(arquivo)", None)),
        ({"lancamentos": VALIDO["lancamentos"]}, ("versao", None)),
        ({"versao": "1"}, ("lancamentos", None)),
    ],
    ids=["lista_na_raiz", "sem_versao", "sem_lancamentos"],
)
def test_raiz(documento, esperado):
    erros = _erros(documento)

    assert [(caminho, numero) for caminho, numero, _ in erros] == [esperado]


@pytest.mark.parametrize(
    ("versao", "trecho"),
    [
        ("2", 'versão "2" não é aceita: use o prompt atual do Ledgr'),
        (1, 'use o texto "1", entre aspas'),
        ("1.0", 'versão "1.0" não é aceita'),
        (None, "versão null não é aceita"),
    ],
)
def test_versao_desconhecida(versao, trecho):
    # Os lançamentos de outra versão não são validados pelo v1.
    erros = _erros({"versao": versao, "lancamentos": [{"quantia": 1}]})

    assert len(erros) == 1
    assert erros[0][0] == "versao"
    assert trecho in erros[0][2]


def test_campo_desconhecido_na_raiz_e_no_lancamento():
    documento = {**_com(saldo="10.00"), "total": "10.00"}

    erros = _erros(documento)

    assert ("total", None) in [(c, n) for c, n, _ in erros]
    assert ("lancamentos[0].saldo", 1) in [(c, n) for c, n, _ in erros]
    mensagens = {caminho: mensagem for caminho, _, mensagem in erros}
    assert mensagens["lancamentos[0].saldo"] == (
        "campo não aceito: aqui o Ledgr só lê data, valor, descricao"
    )


def test_campo_repetido():
    conteudo = (
        b'{"versao": "1", "lancamentos": [{"data": "2026-09-05", "valor": "1.00", '
        b'"valor": "2.00", "descricao": "x"}]}'
    )

    assert _erros(conteudo) == [("lancamentos[0].valor", 1, "campo repetido: deixe só um")]


@pytest.mark.parametrize(
    "lancamentos", ["nada", [], {"data": "2026-09-05"}], ids=["texto", "vazia", "objeto"]
)
def test_lancamentos_fora_de_lista_ou_vazia(lancamentos):
    (erro,) = _erros({"versao": "1", "lancamentos": lancamentos})

    assert erro[0] == "lancamentos"


def test_lancamento_que_nao_e_objeto():
    erros = _erros({"versao": "1", "lancamentos": [VALIDO["lancamentos"][0], "x", 3]})

    assert [(c, n) for c, n, _ in erros] == [("lancamentos[1]", 2), ("lancamentos[2]", 3)]


def test_campo_obrigatorio():
    erros = _erros({"versao": "1", "lancamentos": [{"data": "2026-09-05"}]})

    assert erros == [
        ("lancamentos[0].valor", 1, "campo obrigatório"),
        ("lancamentos[0].descricao", 1, "campo obrigatório"),
    ]


# --- cada campo -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "trecho"),
    [
        ("05/09/2026", 'use o formato AAAA-MM-DD, como "2026-09-05" (veio "05/09/2026")'),
        ("2026-9-5", "use o formato AAAA-MM-DD"),
        ("2026-09-05T10:00:00", "use o formato AAAA-MM-DD"),
        ("2026-02-30", 'essa data não existe (veio "2026-02-30")'),
        (20260905, "use texto entre aspas no formato AAAA-MM-DD"),
        (None, "(veio null)"),
    ],
)
def test_data_invalida(data, trecho):
    (erro,) = _erros(_com(data=data))

    assert erro[:2] == ("lancamentos[0].data", 1)
    assert trecho in erro[2]


@pytest.mark.parametrize(
    ("valor", "trecho"),
    [
        (-1234.56, 'use o número entre aspas, como "-1234.56" (veio -1234.56, sem aspas)'),
        (100, "(veio 100, sem aspas)"),
        ("1.234,56", "use número com ponto decimal e no máximo duas casas"),
        ("1234,56", '(veio "1234,56")'),
        ("R$ 10.00", "sem separador de milhar nem R$"),
        ("(1234.56)", '(veio "(1234.56)")'),
        ("1234.56-", '(veio "1234.56-")'),
        ("+10.00", "use número com ponto decimal"),
        ("10.001", "no máximo duas casas"),
        ("1e3", "use número com ponto decimal"),
        (" 10.00", "use número com ponto decimal"),
        ("", "use número com ponto decimal"),
        ("1234567890123", "valor grande demais"),
        ("0", "valor zero não é lançamento"),
        ("-0.00", "valor zero não é lançamento"),
        (True, "(veio true)"),
    ],
)
def test_valor_invalido(valor, trecho):
    (erro,) = _erros(_com(valor=valor))

    assert erro[:2] == ("lancamentos[0].valor", 1)
    assert trecho in erro[2]


def test_valor_no_limite_da_coluna():
    assert ler_importacao(_bytes(_com(valor="-999999999999.99")))[0].valor == Decimal(
        "-999999999999.99"
    )


@pytest.mark.parametrize(
    ("descricao", "trecho"),
    [
        ("", "a descrição está vazia"),
        ("   ", "a descrição está vazia"),
        ("x" * 501, "passa de 500 caracteres"),
        (123, "use texto entre aspas (veio 123)"),
    ],
)
def test_descricao_invalida(descricao, trecho):
    (erro,) = _erros(_com(descricao=descricao))

    assert erro[:2] == ("lancamentos[0].descricao", 1)
    assert trecho in erro[2]


def test_mensagem_nunca_repete_a_descricao():
    secreta = "Pix para Maria da Silva CPF 123.456.789-00"
    documento = _com(descricao=secreta, data="ontem", valor="dez", extra=secreta)

    erros = _erros(documento)

    assert len(erros) == 3
    assert all(secreta not in mensagem for _, _, mensagem in erros)


def test_eco_longo_e_cortado():
    (erro,) = _erros(_com(valor="9" * 80 + ","))

    # 30 caracteres no eco, contando a aspa e as reticências.
    assert '(veio "' + "9" * 28 + "…)" in erro[2]


# --- todos os erros, até o limite -----------------------------------------------------


def test_junta_todos_os_erros_ate_o_limite():
    lancamentos = [
        {"data": "05/09/2026", "valor": -1.0, "descricao": ""} for _ in range(MAXIMO_ERROS)
    ]

    with pytest.raises(ArquivoForaDoSchema) as erro:
        ler_importacao(_bytes({"versao": "1", "lancamentos": lancamentos}))

    assert erro.value.total == 3 * MAXIMO_ERROS
    assert len(erro.value.erros) == MAXIMO_ERROS
    assert [e.caminho for e in erro.value.erros[:4]] == [
        "lancamentos[0].data",
        "lancamentos[0].valor",
        "lancamentos[0].descricao",
        "lancamentos[1].data",
    ]
    assert erro.value.erros[3].lancamento == 2


def test_um_lancamento_ruim_rejeita_o_arquivo_inteiro():
    documento = {
        "versao": "1",
        "lancamentos": [*VALIDO["lancamentos"], {**VALIDO["lancamentos"][0], "valor": "x"}],
    }

    assert [(c, n) for c, n, _ in _erros(documento)] == [("lancamentos[3].valor", 4)]


# --- prompt e schema ------------------------------------------------------------------


def test_prompt_registra_as_duas_versoes():
    prompt = prompt_atual()

    assert (prompt.versao_schema, prompt.versao_prompt) == ("1", "1")
    assert prompt.texto.startswith("Você vai converter")
    assert "<!--" not in prompt.texto
    for trecho in (
        "Não resuma, não abrevie e não use reticências",
        'Valor entre parênteses, como (1.234,56), é saída: escreva "-1234.56"',
        "Sinal de menos depois do número, como 1.234,56-, também é saída",
    ):
        assert trecho in prompt.texto


def test_schema_bate_com_a_leitura():
    schema = schema_atual()
    item = schema["properties"]["lancamentos"]["items"]

    assert schema["additionalProperties"] is False and item["additionalProperties"] is False
    assert schema["properties"]["versao"] == {"const": "1"}
    assert set(item["required"]) == {"data", "valor", "descricao"}
    assert item["properties"]["descricao"]["maxLength"] == 500
    # O exemplo do prompt passa na leitura.
    exemplo = prompt_atual().texto.split("\n\n")[2]
    assert len(ler_importacao(exemplo.encode())) == 1
