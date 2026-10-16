"""Consulta de CNPJ pela BrasilAPI, `GET /api/cnpj/v1/{cnpj}` (issue #119).

Por que a BrasilAPI (comparação completa no PR da #119): não pede chave nem
cadastro, não tem limite fixo por minuto (pede só uso com "natureza de uma
pessoa real", o caso do cadastro, ainda mais com o cache de 24 horas), separa
"não encontrado" (404) de "fora do ar" (503), devolve a situação cadastral e a
razão social, e a documentação aceita CNPJ alfanumérico. A API pública da
ReceitaWS e a do CNPJ.ws limitam a 3 consultas por minuto, e a da ReceitaWS
só responde CNPJ que já está no cache dela. Os dados da BrasilAPI vêm da base
aberta da Receita, extraída por mês (via Minha Receita): um CNPJ aberto depois
da última extração ainda volta como não encontrado.

Sem SDK, só `httpx`, e sem repetição, como os clientes da OpenAI (ADR-011) e
do Resend (ADR-012). O timeout é total: a chamada roda numa thread e quem
consulta espera no máximo `timeout_segundos` (5 por padrão), mesmo que a
conexão ou a leitura demorem por partes. Uma chamada que estoura o prazo
termina sozinha depois, limitada pelo mesmo timeout do `httpx`.

Mapeamento, sempre categorizado (app/services/consulta_cnpj/base.py):
- 200 com `razao_social` e `descricao_situacao_cadastral` em texto: "ativa"
  se a situação é ATIVA, senão "inativa". Corpo fora disso, ou com outro
  `cnpj` que não o consultado, vira "indisponivel".
- 404 com o corpo de erro da BrasilAPI (`type` "not_found"): "nao_encontrado".
  Um 404 sem esse corpo (URL errada na configuração, página de erro do
  servidor) vira "indisponivel", para nunca barrar cadastro por engano.
- 400 (CNPJ recusado pelo formato, como um alfanumérico que a API ainda não
  aceite), 429, 5xx, qualquer outro status, timeout e erro de rede:
  "indisponivel".

Nunca loga o CNPJ, a razão social, o corpo nem a chave: só o motivo
categorizado da falha, o código HTTP e a duração.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as PrazoEsgotado
from typing import Any

import httpx

from app.services.consulta_cnpj.base import (
    INDISPONIVEL,
    NAO_ENCONTRADO,
    ResultadoConsultaCNPJ,
)

logger = logging.getLogger(__name__)

TIMEOUT_TOTAL_SEGUNDOS = 5.0

# Threads das chamadas, para o prazo total valer para quem espera. Poucas: o
# cadastro tem rate limit e cache, e uma chamada presa termina pelo timeout.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="consulta-cnpj")

_FORA_DO_CNPJ = re.compile(r"[^0-9A-Za-z]")


def _texto_preenchido(valor: Any) -> str | None:
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return None


def _interpretar(corpo: Any, cnpj: str) -> ResultadoConsultaCNPJ | None:
    """Resultado a partir do corpo de um 200, ou `None` se o formato não é o
    esperado (nunca levanta por causa do formato)."""
    if not isinstance(corpo, dict):
        return None
    razao_social = _texto_preenchido(corpo.get("razao_social"))
    situacao = _texto_preenchido(corpo.get("descricao_situacao_cadastral"))
    if razao_social is None or situacao is None:
        return None
    devolvido = corpo.get("cnpj")
    if devolvido is not None and (
        not isinstance(devolvido, str) or _FORA_DO_CNPJ.sub("", devolvido).upper() != cnpj
    ):
        return None
    return ResultadoConsultaCNPJ(
        situacao="ativa" if situacao.upper() == "ATIVA" else "inativa",
        razao_social=razao_social,
    )


def _e_nao_encontrado(resposta: httpx.Response) -> bool:
    try:
        corpo = resposta.json()
    except ValueError:
        return False
    return isinstance(corpo, dict) and corpo.get("type") == "not_found"


class ConsultaBrasilAPI:
    """Implementação de `ConsultaCNPJ` pela BrasilAPI. `cliente_http` é
    injetável para teste (`httpx.MockTransport`); sem ele, cada chamada abre e
    fecha o próprio `httpx.Client`. `chave` vai como `Authorization: Bearer`
    só quando preenchida: a BrasilAPI não usa chave, e o campo fica para um
    provedor que use."""

    def __init__(
        self,
        base_url: str,
        chave: str = "",
        timeout_segundos: float = TIMEOUT_TOTAL_SEGUNDOS,
        cliente_http: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._chave = chave
        self._timeout_segundos = timeout_segundos
        self._cliente_http = cliente_http

    def consultar(self, cnpj: str) -> ResultadoConsultaCNPJ:
        inicio = time.monotonic()
        futuro = _executor.submit(self._buscar, cnpj, inicio)
        try:
            return futuro.result(timeout=self._timeout_segundos)
        except PrazoEsgotado:
            futuro.cancel()
            return self._falha("timeout", None, inicio)
        except Exception as exc:  # noqa: BLE001 - consultar nunca levanta
            return self._falha(f"erro_{type(exc).__name__}", None, inicio)

    def _buscar(self, cnpj: str, inicio: float) -> ResultadoConsultaCNPJ:
        cabecalhos = {"Accept": "application/json"}
        if self._chave:
            cabecalhos["Authorization"] = f"Bearer {self._chave}"

        cliente = self._cliente_http or httpx.Client()
        try:
            resposta = cliente.get(
                f"{self._base_url}/{cnpj}",
                headers=cabecalhos,
                timeout=httpx.Timeout(self._timeout_segundos),
            )
        except httpx.TimeoutException:
            return self._falha("timeout", None, inicio)
        except (httpx.HTTPError, httpx.InvalidURL):
            # httpx.InvalidURL não é subclasse de HTTPError (mesma nota de
            # app/services/ia/openai_provider.py).
            return self._falha("rede", None, inicio)
        finally:
            if self._cliente_http is None:
                cliente.close()

        if resposta.status_code == 404 and _e_nao_encontrado(resposta):
            return NAO_ENCONTRADO
        if resposta.status_code != 200:
            return self._falha("http", resposta.status_code, inicio)
        try:
            resultado = _interpretar(resposta.json(), cnpj)
        except ValueError:
            resultado = None
        if resultado is None:
            return self._falha("resposta_invalida", 200, inicio)
        return resultado

    @staticmethod
    def _falha(motivo: str, status_http: int | None, inicio: float) -> ResultadoConsultaCNPJ:
        logger.info(
            "consulta_cnpj provedor=brasilapi motivo=%s status_http=%s duracao_ms=%s",
            motivo,
            status_http,
            round((time.monotonic() - inicio) * 1000),
        )
        return INDISPONIVEL
