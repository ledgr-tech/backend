"""Cache em memória do resultado da consulta de CNPJ (issue #119).

Guarda "ativa", "inativa" e "nao_encontrado" por 24 horas, por CNPJ, e nunca
"indisponivel" (a próxima tentativa tem que consultar de novo). Serve para o
`POST /register` logo depois do `POST /register/verificar` não consultar de
novo, e para a rota pública não virar um jeito de gastar a cota da API.

Por processo, como o rate limit e o bloqueio de login: com mais de uma
réplica, cada uma tem o seu. Limitado a `MAXIMO_ENTRADAS`; cheio, sai primeiro
o que já venceu e, se não bastar, o mais antigo.
"""

import threading
import time
from collections.abc import Callable

from app.services.consulta_cnpj.base import ResultadoConsultaCNPJ

VALIDADE_SEGUNDOS = 24 * 60 * 60
MAXIMO_ENTRADAS = 10_000


class CacheConsultaCNPJ:
    def __init__(self, relogio: Callable[[], float] = time.monotonic) -> None:
        self._relogio = relogio
        self._trava = threading.Lock()
        self._entradas: dict[str, tuple[ResultadoConsultaCNPJ, float]] = {}

    def obter(self, cnpj: str) -> ResultadoConsultaCNPJ | None:
        with self._trava:
            entrada = self._entradas.get(cnpj)
            if entrada is None:
                return None
            resultado, vence_em = entrada
            if self._relogio() >= vence_em:
                del self._entradas[cnpj]
                return None
            return resultado

    def guardar(self, cnpj: str, resultado: ResultadoConsultaCNPJ) -> None:
        if resultado.situacao == "indisponivel":
            return
        with self._trava:
            agora = self._relogio()
            self._entradas.pop(cnpj, None)
            if len(self._entradas) >= MAXIMO_ENTRADAS:
                for chave in [c for c, (_, vence) in self._entradas.items() if vence <= agora]:
                    del self._entradas[chave]
            while len(self._entradas) >= MAXIMO_ENTRADAS:
                del self._entradas[next(iter(self._entradas))]
            self._entradas[cnpj] = (resultado, agora + VALIDADE_SEGUNDOS)

    def limpar(self) -> None:
        with self._trava:
            self._entradas.clear()


cache_por_cnpj = CacheConsultaCNPJ()
