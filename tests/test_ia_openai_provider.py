"""Testes de app/services/ia/openai_provider.py (issue #28, ADR-011) —
`httpx.MockTransport` no lugar da API real: nenhum teste aqui faz chamada
de rede de verdade nem precisa de banco.
"""

import json
import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.services.ia.base import ContextoDivergencia, LancamentoContexto, ProvedorIAIndisponivel
from app.services.ia.openai_provider import ProvedorOpenAI
from app.services.ia.prompt import montar_mensagens

API_KEY = "sk-teste-nao-usar-em-producao"


def _contexto() -> ContextoDivergencia:
    return ContextoDivergencia(
        status="divergente_valor",
        motivo="Existe um lançamento do outro lado na mesma data, mas o valor não coincide.",
        lancamento=LancamentoContexto(
            data=date(2026, 9, 20), valor=Decimal("1500.00"), tipo="credito", descricao="a"
        ),
        candidatos=(),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
    )


def _provedor(handler, **kwargs) -> ProvedorOpenAI:
    cliente = httpx.Client(transport=httpx.MockTransport(handler))
    parametros = {
        "api_key": API_KEY,
        "modelo": "gpt-6-luna",
        "base_url": "https://api.openai.com/v1",
        "timeout_segundos": 5.0,
        "max_tokens_saida": 300,
        "cliente_http": cliente,
    }
    parametros.update(kwargs)
    return ProvedorOpenAI(**parametros)


def _resposta_sucesso(**overrides) -> dict:
    corpo = {
        "model": "gpt-6-luna",
        "choices": [
            {"message": {"content": "Explicação gerada."}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 700, "completion_tokens": 42},
    }
    corpo.update(overrides)
    return corpo


# corpo e headers da requisição


def test_corpo_da_requisicao_e_exato_sem_identificador():
    capturadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json=_resposta_sucesso())

    contexto = _contexto()
    provedor = _provedor(handler)
    provedor.explicar_divergencia(contexto)

    requisicao = capturadas[0]
    corpo = json.loads(requisicao.content)

    assert corpo == {
        "model": "gpt-6-luna",
        "messages": montar_mensagens(contexto),
        "reasoning_effort": "none",
        "max_completion_tokens": 300,
        "store": False,
    }
    assert "safety_identifier" not in corpo
    assert "temperature" not in corpo
    assert "max_tokens" not in corpo
    assert requisicao.headers["authorization"] == f"Bearer {API_KEY}"
    assert requisicao.headers["content-type"] == "application/json"
    assert requisicao.url == "https://api.openai.com/v1/chat/completions"


def test_corpo_da_requisicao_inclui_safety_identifier_quando_informado():
    capturadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json=_resposta_sucesso())

    provedor = _provedor(handler)
    provedor.explicar_divergencia(_contexto(), identificador_anonimo="hash-empresa-123")

    corpo = json.loads(capturadas[0].content)
    assert corpo["safety_identifier"] == "hash-empresa-123"


# sucesso


def test_sucesso_devolve_resposta_ia_com_tokens_e_modelo():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_resposta_sucesso())

    provedor = _provedor(handler)
    resposta = provedor.explicar_divergencia(_contexto())

    assert resposta.texto == "Explicação gerada."
    assert resposta.tokens_entrada == 700
    assert resposta.tokens_saida == 42
    assert resposta.modelo == "gpt-6-luna"


def test_sucesso_sem_usage_devolve_tokens_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        corpo = _resposta_sucesso()
        del corpo["usage"]
        return httpx.Response(200, json=corpo)

    provedor = _provedor(handler)
    resposta = provedor.explicar_divergencia(_contexto())

    assert resposta.tokens_entrada == 0
    assert resposta.tokens_saida == 0


def test_sucesso_sem_model_na_resposta_usa_o_configurado():
    def handler(request: httpx.Request) -> httpx.Response:
        corpo = _resposta_sucesso()
        del corpo["model"]
        return httpx.Response(200, json=corpo)

    provedor = _provedor(handler, modelo="gpt-6-luna")
    resposta = provedor.explicar_divergencia(_contexto())

    assert resposta.modelo == "gpt-6-luna"


# falhas


def test_timeout_levanta_indisponivel_com_motivo_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tempo esgotado")

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "timeout"
    assert exc_info.value.status_http is None


def test_erro_de_rede_levanta_indisponivel_com_motivo_http():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada")

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "http"
    assert exc_info.value.status_http is None


def test_status_500_levanta_indisponivel_com_motivo_http_e_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "erro interno"})

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "http"
    assert exc_info.value.status_http == 500


def test_status_429_levanta_indisponivel_com_motivo_http_e_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "limite excedido"})

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "http"
    assert exc_info.value.status_http == 429


def test_json_invalido_levanta_indisponivel_com_motivo_resposta_invalida():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"isto nao e json{{{")

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "resposta_invalida"


def test_choices_vazio_levanta_indisponivel_com_motivo_resposta_invalida():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "gpt-6-luna", "choices": [], "usage": {}})

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "resposta_invalida"


def test_content_vazio_levanta_indisponivel_com_motivo_resposta_invalida():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_resposta_sucesso(
                choices=[{"message": {"content": "   "}, "finish_reason": "stop"}]
            ),
        )

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "resposta_invalida"


def test_finish_reason_length_levanta_indisponivel_com_motivo_resposta_invalida():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_resposta_sucesso(
                choices=[{"message": {"content": "truncado no meio"}, "finish_reason": "length"}]
            ),
        )

    provedor = _provedor(handler)

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "resposta_invalida"


def test_sem_chave_levanta_nao_configurado_sem_requisicao():
    chamadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        raise AssertionError("não deveria fazer nenhuma requisição sem chave configurada")

    provedor = _provedor(handler, api_key="")

    with pytest.raises(ProvedorIAIndisponivel) as exc_info:
        provedor.explicar_divergencia(_contexto())
    assert exc_info.value.motivo == "nao_configurado"
    assert chamadas == []


# logs — nunca descrição, texto da resposta ou chave


def test_log_de_sucesso_nao_contem_descricao_resposta_nem_chave(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_resposta_sucesso())

    provedor = _provedor(handler)
    with caplog.at_level(logging.INFO):
        resposta = provedor.explicar_divergencia(_contexto())

    texto_dos_logs = "\n".join(registro.getMessage() for registro in caplog.records)
    assert API_KEY not in texto_dos_logs
    assert resposta.texto not in texto_dos_logs
    assert "credito" not in texto_dos_logs  # descrição/tipo do lançamento
    assert "provedor=openai" in texto_dos_logs
    assert "modelo=gpt-6-luna" in texto_dos_logs
    assert "status_http=200" in texto_dos_logs
    assert "tokens_entrada=700" in texto_dos_logs
    assert "tokens_saida=42" in texto_dos_logs


def test_log_de_falha_nao_contem_descricao_nem_chave(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "erro interno"})

    provedor = _provedor(handler)
    with caplog.at_level(logging.INFO), pytest.raises(ProvedorIAIndisponivel):
        provedor.explicar_divergencia(_contexto())

    texto_dos_logs = "\n".join(registro.getMessage() for registro in caplog.records)
    assert API_KEY not in texto_dos_logs
    assert "erro interno" not in texto_dos_logs
    assert "status_http=500" in texto_dos_logs
