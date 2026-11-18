"""O modo com dados do assistente no provedor (issue #223, ADR-028).

A OpenAI só interpreta a pergunta: devolve o tipo (uso, dados, ação ou fora)
e, nas perguntas de dados, a consulta e os parâmetros. Nada da empresa vai na
chamada. `httpx.MockTransport` no lugar da API real.
"""

import json

import httpx
import pytest

from app.services import api_publica
from app.services.ia.base import ConsultaDeDados, PerguntaAjuda, ProvedorIAIndisponivel
from app.services.ia.openai_provider import ProvedorOpenAI
from app.services.ia.prompt_ajuda import (
    CATEGORIAS,
    INSTRUCAO_SISTEMA_AJUDA,
    INSTRUCAO_SISTEMA_DADOS,
    INTENCOES,
    SCHEMA_RESPOSTA,
    SCHEMA_RESPOSTA_DADOS,
    TELAS_DE_ACAO,
    ler_resposta_dados,
    montar_mensagens_ajuda,
)


def _pergunta(**kwargs) -> PerguntaAjuda:
    campos = {
        "central": '{"versao":"v","paginas":[]}',
        "pergunta": "como ficou setembro?",
        "historico": (),
        "tela": None,
        "dados": True,
        "mes_atual": "2026-11",
        **kwargs,
    }
    return PerguntaAjuda(**campos)


def _provedor(handler) -> ProvedorOpenAI:
    return ProvedorOpenAI(
        api_key="sk-teste-nao-usar-em-producao",
        modelo="gpt-6-luna",
        base_url="https://api.openai.com/v1",
        timeout_segundos=5.0,
        max_tokens_saida=300,
        cliente_http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_tokens_ajuda=500,
    )


def _resposta(conteudo: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-6-luna",
            "choices": [{"message": {"content": conteudo}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9000, "completion_tokens": 40},
        },
    )


def _saida(**kwargs) -> str:
    campos = {
        "tipo": "dados",
        "resposta": "",
        "respondida": True,
        "citacoes": [],
        "intencao": "resumo_do_mes",
        "competencia": "2026-09",
        "meses_atras": None,
        "categoria": None,
        "tela": None,
        **kwargs,
    }
    return json.dumps(campos)


def test_pergunta_de_dados_volta_como_consulta_sem_texto_do_modelo():
    pedidos = []

    def handler(request):
        pedidos.append(json.loads(request.content))
        return _resposta(_saida())

    resposta = _provedor(handler).responder_ajuda(_pergunta())

    assert resposta.tipo == "dados"
    assert resposta.consulta == ConsultaDeDados(
        intencao="resumo_do_mes", competencia="2026-09", meses_atras=None, categoria=None
    )
    (corpo,) = pedidos
    formato = corpo["response_format"]["json_schema"]
    assert formato["strict"] is True and formato["schema"] == SCHEMA_RESPOSTA_DADOS
    assert corpo["messages"][0] == {"role": "system", "content": INSTRUCAO_SISTEMA_DADOS}


def test_sem_modo_dados_a_chamada_continua_a_do_adr_020():
    pedidos = []

    def handler(request):
        pedidos.append(json.loads(request.content))
        return _resposta(
            json.dumps({"resposta": "Use a tela Extratos.", "respondida": True, "citacoes": []})
        )

    resposta = _provedor(handler).responder_ajuda(_pergunta(dados=False, mes_atual=None))

    assert (resposta.tipo, resposta.consulta, resposta.resposta) == (
        "uso",
        None,
        "Use a tela Extratos.",
    )
    (corpo,) = pedidos
    assert corpo["response_format"]["json_schema"]["schema"] == SCHEMA_RESPOSTA
    assert corpo["messages"][0] == {"role": "system", "content": INSTRUCAO_SISTEMA_AJUDA}
    assert not any("mes_atual" in m["content"] for m in corpo["messages"])


def test_pergunta_de_uso_no_modo_dados_traz_resposta_e_citacoes():
    saida = _saida(
        tipo="uso",
        resposta="1. Abra Fechamentos.",
        citacoes=[{"pagina": "fechamentos", "funcao": "fechar-mes"}],
        intencao=None,
        competencia=None,
    )
    resposta = _provedor(lambda request: _resposta(saida)).responder_ajuda(_pergunta())

    assert (resposta.tipo, resposta.resposta, resposta.respondida) == (
        "uso",
        "1. Abra Fechamentos.",
        True,
    )
    assert resposta.citacoes == (("fechamentos", "fechar-mes"),)
    assert resposta.consulta is None


def test_pedido_de_gravar_volta_como_acao_com_a_tela():
    saida = _saida(tipo="acao", intencao=None, competencia=None, tela="fechamentos")
    resposta = _provedor(lambda request: _resposta(saida)).responder_ajuda(_pergunta())

    assert (resposta.tipo, resposta.tela, resposta.consulta) == ("acao", "fechamentos", None)


@pytest.mark.parametrize(
    "conteudo",
    [
        "não é json",
        _saida(tipo="outro"),
        _saida(intencao="apagar_tudo"),
        _saida(categoria="inventada"),
        _saida(tela="inventada"),
        _saida(meses_atras="um"),
        _saida(tipo="uso", resposta="   ", intencao=None),
        json.dumps({"resposta": "x", "respondida": True, "citacoes": []}),
    ],
)
def test_saida_fora_do_formato_e_resposta_invalida(conteudo):
    with pytest.raises(ProvedorIAIndisponivel) as erro:
        _provedor(lambda request: _resposta(conteudo)).responder_ajuda(_pergunta())
    assert erro.value.motivo == "resposta_invalida"


def test_dados_sem_intencao_vira_fora():
    """O modelo marcou dados mas não disse qual consulta: o backend não chuta."""
    assert ler_resposta_dados(_saida(intencao=None)).tipo == "fora"


def test_mensagens_do_modo_dados_levam_o_mes_atual_e_nada_alem_da_conversa():
    mensagens = montar_mensagens_ajuda(_pergunta(pergunta="e o mês passado?"))

    assert mensagens[0]["content"] == INSTRUCAO_SISTEMA_DADOS
    assert mensagens[-1]["content"] == (
        "<mes_atual>2026-11</mes_atual>\n<pergunta>\ne o mês passado?\n</pergunta>"
    )


def test_schema_dos_dados_e_estrito_com_todos_os_campos_obrigatorios():
    propriedades = SCHEMA_RESPOSTA_DADOS["properties"]

    assert SCHEMA_RESPOSTA_DADOS["additionalProperties"] is False
    assert set(SCHEMA_RESPOSTA_DADOS["required"]) == set(propriedades)
    assert propriedades["intencao"]["enum"] == [*INTENCOES, None]
    assert propriedades["categoria"]["enum"] == [*CATEGORIAS, None]
    assert propriedades["tela"]["enum"] == [*TELAS_DE_ACAO, None]


def test_categorias_sao_as_da_api_publica():
    assert CATEGORIAS == api_publica.CATEGORIAS_DE_DIVERGENCIA


def test_instrucao_diz_que_o_assistente_so_consulta_e_nao_calcula():
    texto = INSTRUCAO_SISTEMA_DADOS
    assert "nunca grava" in texto
    assert "Não responda nem calcule" in texto
    for intencao in INTENCOES:
        assert intencao in texto


def test_comparar_meses_traz_o_segundo_mes():
    resposta = ler_resposta_dados(
        _saida(
            intencao="comparar_meses",
            competencia="2026-10",
            comparar_com=None,
            comparar_com_meses_atras=2,
        )
    )

    assert resposta.consulta.intencao == "comparar_meses"
    assert resposta.consulta.competencia == "2026-10"
    assert resposta.consulta.comparar_com is None
    assert resposta.consulta.comparar_com_meses_atras == 2


def test_segundo_mes_com_tipo_errado_e_falha_do_provedor():
    assert ler_resposta_dados(_saida(comparar_com_meses_atras="2")) is None
    assert ler_resposta_dados(_saida(comparar_com=202610)) is None
