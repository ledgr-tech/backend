"""Cliente do Resend via `POST /emails` (issue #66, ADR-012).

Sem SDK oficial: só `httpx`, como o cliente da OpenAI (ADR-011). Sem retry
automático: qualquer falha (timeout, erro de rede/URL, status fora de 2xx)
levanta `ProvedorEmailIndisponivel`, categorizada por motivo. O corpo da
resposta de sucesso (`{"id": ...}`) não é usado.

Nunca loga destinatário, assunto, corpo, link nem a chave — só provedor,
código HTTP e duração em ms, em nível INFO, em sucesso e em falha.
"""

import logging
import time

import httpx

from app.services.email.base import MensagemEmail, ProvedorEmailIndisponivel

logger = logging.getLogger(__name__)


class ProvedorResend:
    """Implementação de `ProvedorDeEmail` pro Resend (ADR-012).
    `cliente_http` é injetável pra teste (`httpx.MockTransport`) — sem
    injeção, cria e fecha um `httpx.Client` próprio por chamada."""

    def __init__(
        self,
        api_key: str,
        remetente: str,
        base_url: str,
        timeout_segundos: float,
        responder_para: str = "",
        cliente_http: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._remetente = remetente
        self._base_url = base_url.rstrip("/")
        self._timeout_segundos = timeout_segundos
        self._responder_para = responder_para
        self._cliente_http = cliente_http

    def enviar(self, mensagem: MensagemEmail) -> None:
        if not self._api_key:
            raise ProvedorEmailIndisponivel("nao_configurado")

        corpo: dict = {
            "from": self._remetente,
            "to": [mensagem.para],
            "subject": mensagem.assunto,
            "text": mensagem.texto,
            "html": mensagem.html,
        }
        if self._responder_para:
            corpo["reply_to"] = self._responder_para

        cabecalhos = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if mensagem.chave_idempotencia:
            cabecalhos["Idempotency-Key"] = mensagem.chave_idempotencia

        cliente = self._cliente_http or httpx.Client()
        inicio = time.monotonic()
        try:
            resposta = cliente.post(
                f"{self._base_url}/emails",
                json=corpo,
                headers=cabecalhos,
                timeout=httpx.Timeout(self._timeout_segundos),
            )
        except httpx.TimeoutException as exc:
            self._logar(status_http=None, inicio=inicio)
            raise ProvedorEmailIndisponivel("timeout") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx.InvalidURL não é subclasse de HTTPError (mesma nota de
            # app/services/ia/openai_provider.py).
            self._logar(status_http=None, inicio=inicio)
            raise ProvedorEmailIndisponivel("http") from exc
        finally:
            if self._cliente_http is None:
                cliente.close()

        self._logar(status_http=resposta.status_code, inicio=inicio)
        if not resposta.is_success:
            raise ProvedorEmailIndisponivel("http", status_http=resposta.status_code)

    @staticmethod
    def _logar(*, status_http: int | None, inicio: float) -> None:
        logger.info(
            "provedor=resend status_http=%s duracao_ms=%s",
            status_http,
            round((time.monotonic() - inicio) * 1000),
        )
