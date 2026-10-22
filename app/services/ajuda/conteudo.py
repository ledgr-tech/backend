"""Conteúdo da Central de ajuda para o assistente (issue #137, ADR-020).

A Central de ajuda é escrita no front (`app/(ajuda)/ajuda/paginas.ts`, front
#171), uma página por tela, e o build do front publica um JSON gerado dela
(front #172) em `AJUDA_CONTEUDO_URL`. O backend não guarda cópia própria:
busca o JSON com httpx (timeout curto), valida com os modelos abaixo e o
guarda em memória por `VALIDADE_CACHE`. Se a busca seguinte falhar (rede,
status, JSON inválido ou fora do formato), mantém a última cópia válida e só
tenta de novo depois de outra validade. Sem nenhuma cópia válida,
`ConteudoIndisponivel`, e o assistente responde 503; nesse caso a próxima
tentativa vem em `INTERVALO_SEM_COPIA`, para um deploy do backend antes do
front se resolver logo, sem buscar a cada pergunta.
Nada disso roda no boot: a primeira busca é na primeira pergunta.

`serializar` é o texto da Central que vai no contexto do modelo: JSON
canônico, com `<` e `>` escapados para nenhum texto fechar o delimitador
(como em app/services/ia/prompt.py). Acima de
`ASSISTENTE_MAXIMO_CONTEUDO_CARACTERES`, `ConteudoGrandeDemais`, com log de
erro: a Central inteira vai no contexto enquanto couber, e a busca por trecho
fica para quando ela crescer.

A seção de cada função está em `/ajuda/<pagina>#<funcao>`; `caminho` monta
esse endereço.
"""

import json
import logging
import threading
import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

VALIDADE_CACHE = 10 * 60  # segundos
INTERVALO_SEM_COPIA = 30  # segundos
TIMEOUT_BUSCA = 3.0  # segundos

Identificador = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


class _Modelo(BaseModel):
    # Campos a mais são ignorados: o front pode acrescentar campos sem quebrar
    # o backend; os que faltam ou vêm com o tipo errado invalidam o JSON.
    model_config = ConfigDict(extra="ignore", frozen=True)


class FuncaoAjuda(_Modelo):
    id: str = Identificador
    titulo: str = Field(min_length=1)
    faz: str
    passos: list[str] | None = None
    nota: str | None = None


class PaginaAjuda(_Modelo):
    id: str = Identificador
    titulo: str = Field(min_length=1)
    onde: str
    serve: str
    funcoes: list[FuncaoAjuda]


class CentralDeAjuda(_Modelo):
    versao: str = Field(min_length=1)
    paginas: list[PaginaAjuda]

    def funcao(self, pagina_id: str, funcao_id: str) -> FuncaoAjuda | None:
        for pagina in self.paginas:
            if pagina.id == pagina_id:
                return next((f for f in pagina.funcoes if f.id == funcao_id), None)
        return None

    def tem_pagina(self, pagina_id: str) -> bool:
        return any(pagina.id == pagina_id for pagina in self.paginas)


def caminho(pagina_id: str, funcao_id: str) -> str:
    return f"/ajuda/{pagina_id}#{funcao_id}"


class ConteudoIndisponivel(Exception):
    """Nenhuma cópia válida da Central de ajuda."""


class ConteudoGrandeDemais(Exception):
    """A Central serializada passou do tamanho que cabe no contexto."""


def serializar(central: CentralDeAjuda, maximo_caracteres: int) -> str:
    texto = json.dumps(
        central.model_dump(exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    texto = texto.replace("<", "\\u003c").replace(">", "\\u003e")
    if len(texto) > maximo_caracteres:
        logger.error(
            "assistente conteudo_grande_demais caracteres=%d maximo=%d",
            len(texto),
            maximo_caracteres,
        )
        raise ConteudoGrandeDemais
    return texto


class FonteDaAjuda:
    """Busca o JSON da Central, com cache em memória e a última cópia válida.
    `cliente_http` e `relogio` são injetáveis para teste."""

    def __init__(
        self,
        url: Callable[[], str],
        cliente_http: httpx.Client | None = None,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._cliente_http = cliente_http
        self._relogio = relogio
        self._trava = threading.Lock()
        self._central: CentralDeAjuda | None = None
        self._buscado_em: float | None = None

    def limpar(self) -> None:
        with self._trava:
            self._central = None
            self._buscado_em = None

    def obter(self) -> CentralDeAjuda:
        with self._trava:
            agora = self._relogio()
            intervalo = VALIDADE_CACHE if self._central is not None else INTERVALO_SEM_COPIA
            if self._buscado_em is None or agora - self._buscado_em >= intervalo:
                # Mesmo quando falha, espera o intervalo para tentar de novo:
                # uma URL fora do ar não vira uma busca por pergunta.
                self._buscado_em = agora
                nova = self._buscar()
                if nova is not None:
                    self._central = nova
            if self._central is None:
                raise ConteudoIndisponivel
            return self._central

    def _buscar(self) -> CentralDeAjuda | None:
        url = self._url().strip()
        if not url:
            logger.info("assistente conteudo resultado=sem_url")
            return None
        cliente = self._cliente_http or httpx.Client()
        try:
            resposta = cliente.get(url, timeout=httpx.Timeout(TIMEOUT_BUSCA))
            if resposta.status_code != 200:
                logger.info(
                    "assistente conteudo resultado=http status_http=%s", resposta.status_code
                )
                return None
            central = CentralDeAjuda.model_validate_json(resposta.content)
        except httpx.HTTPError as exc:
            logger.info("assistente conteudo resultado=erro_de_rede classe=%s", type(exc).__name__)
            return None
        except (ValidationError, ValueError):
            logger.info("assistente conteudo resultado=formato_invalido")
            return None
        finally:
            if self._cliente_http is None:
                cliente.close()
        logger.info("assistente conteudo resultado=atualizado paginas=%d", len(central.paginas))
        return central


def _url_configurada() -> str:
    from app.core.config import settings

    return settings.ajuda_conteudo_url


# A fonte do processo; os testes trocam pela dependency abaixo.
FONTE = FonteDaAjuda(url=_url_configurada)


def obter_fonte_da_ajuda() -> FonteDaAjuda:
    return FONTE
