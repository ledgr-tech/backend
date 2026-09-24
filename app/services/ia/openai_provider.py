"""Cliente do GPT-6 Luna via OpenAI Chat Completions (issue #28, ADR-011).

Sem SDK oficial: só `httpx`, que já é dependência do projeto — uma única
chamada HTTP não justifica dependência nova. Sem retry automático: qualquer
falha (timeout, erro de rede, status HTTP != 200 ou resposta inesperada)
sempre levanta `ProvedorIAIndisponivel`, categorizada por motivo, pra quem
chama decidir o fallback (PR B).

Nunca loga prompt, resposta do modelo, descrição nem a chave — só provedor,
modelo, código HTTP, duração em ms e contagem de tokens, em nível INFO,
tanto em sucesso quanto em falha.
"""

import logging
import time

import httpx

from app.services.ia.base import ContextoDivergencia, ProvedorIAIndisponivel, RespostaIA
from app.services.ia.prompt import montar_mensagens

logger = logging.getLogger(__name__)


class ProvedorOpenAI:
    """Implementação de `ProvedorDeIA` pro GPT-6 Luna (ADR-011).
    `cliente_http` é injetável pra teste (`httpx.MockTransport`) — sem
    injeção, cria e fecha um `httpx.Client` próprio por chamada."""

    def __init__(
        self,
        api_key: str,
        modelo: str,
        base_url: str,
        timeout_segundos: float,
        max_tokens_saida: int,
        cliente_http: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._modelo = modelo
        self._base_url = base_url.rstrip("/")
        self._timeout_segundos = timeout_segundos
        self._max_tokens_saida = max_tokens_saida
        self._cliente_http = cliente_http

    def explicar_divergencia(
        self, contexto: ContextoDivergencia, *, identificador_anonimo: str | None = None
    ) -> RespostaIA:
        if not self._api_key:
            raise ProvedorIAIndisponivel("nao_configurado")

        corpo: dict = {
            "model": self._modelo,
            "messages": montar_mensagens(contexto),
            "reasoning_effort": "none",
            "max_completion_tokens": self._max_tokens_saida,
            "store": False,
        }
        if identificador_anonimo is not None:
            corpo["safety_identifier"] = identificador_anonimo

        cliente = self._cliente_http or httpx.Client()
        cabecalhos = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        inicio = time.monotonic()
        try:
            resposta = cliente.post(
                f"{self._base_url}/chat/completions",
                json=corpo,
                headers=cabecalhos,
                timeout=httpx.Timeout(self._timeout_segundos),
            )
        except httpx.TimeoutException as exc:
            self._logar(status_http=None, duracao_ms=self._duracao_ms(inicio))
            raise ProvedorIAIndisponivel("timeout") from exc
        except httpx.HTTPError as exc:
            self._logar(status_http=None, duracao_ms=self._duracao_ms(inicio))
            raise ProvedorIAIndisponivel("http") from exc
        finally:
            if self._cliente_http is None:
                cliente.close()

        duracao_ms = self._duracao_ms(inicio)

        if resposta.status_code != 200:
            self._logar(status_http=resposta.status_code, duracao_ms=duracao_ms)
            raise ProvedorIAIndisponivel("http", status_http=resposta.status_code)

        try:
            corpo_resposta = resposta.json()
        except ValueError as exc:
            self._logar(status_http=resposta.status_code, duracao_ms=duracao_ms)
            raise ProvedorIAIndisponivel("resposta_invalida") from exc

        escolhas = corpo_resposta.get("choices") or []
        if not escolhas:
            self._logar(status_http=resposta.status_code, duracao_ms=duracao_ms)
            raise ProvedorIAIndisponivel("resposta_invalida")

        primeira = escolhas[0]
        if primeira.get("finish_reason") == "length":
            self._logar(status_http=resposta.status_code, duracao_ms=duracao_ms)
            raise ProvedorIAIndisponivel("resposta_invalida")

        texto = ((primeira.get("message") or {}).get("content") or "").strip()
        if not texto:
            self._logar(status_http=resposta.status_code, duracao_ms=duracao_ms)
            raise ProvedorIAIndisponivel("resposta_invalida")

        uso = corpo_resposta.get("usage") or {}
        tokens_entrada = uso.get("prompt_tokens", 0)
        tokens_saida = uso.get("completion_tokens", 0)
        modelo_resposta = corpo_resposta.get("model") or self._modelo

        self._logar(
            status_http=resposta.status_code,
            duracao_ms=duracao_ms,
            tokens_entrada=tokens_entrada,
            tokens_saida=tokens_saida,
        )
        return RespostaIA(
            texto=texto,
            tokens_entrada=tokens_entrada,
            tokens_saida=tokens_saida,
            modelo=modelo_resposta,
        )

    @staticmethod
    def _duracao_ms(inicio: float) -> int:
        return round((time.monotonic() - inicio) * 1000)

    def _logar(
        self,
        *,
        status_http: int | None,
        duracao_ms: int,
        tokens_entrada: int = 0,
        tokens_saida: int = 0,
    ) -> None:
        logger.info(
            "provedor=openai modelo=%s status_http=%s duracao_ms=%s "
            "tokens_entrada=%s tokens_saida=%s",
            self._modelo,
            status_http,
            duracao_ms,
            tokens_entrada,
            tokens_saida,
        )
