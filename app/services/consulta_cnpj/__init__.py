"""Consulta de CNPJ na Receita (issue #119): fábrica do provedor, cache e log.

`obter_consulta_cnpj` decide, pela configuração, se há provedor: `None` com
`CNPJ_CONSULTA_HABILITADA` falso (o padrão, como o e-mail e a IA) ou sem
`CNPJ_CONSULTA_URL`. `consultar_cnpj` é o que as rotas chamam: usa o cache
de 24 horas (app/services/consulta_cnpj/cache.py), e sem provedor devolve
"indisponivel" sem consultar nada. Import de `settings` é preguiçoso, pelo
mesmo motivo de app/services/email/__init__.py.

Log: uma linha por consulta, `consulta_cnpj resultado=<categoria>
origem=<api|cache|desligada> duracao_ms=<n>`. Nunca o CNPJ nem a razão social.
"""

import logging
import time
from typing import TYPE_CHECKING

from app.services.consulta_cnpj.base import (
    INDISPONIVEL,
    ConsultaCNPJ,
    ResultadoConsultaCNPJ,
    SituacaoCNPJ,
)
from app.services.consulta_cnpj.brasilapi import ConsultaBrasilAPI
from app.services.consulta_cnpj.cache import cache_por_cnpj

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "ConsultaBrasilAPI",
    "ConsultaCNPJ",
    "ResultadoConsultaCNPJ",
    "SituacaoCNPJ",
    "consultar_cnpj",
    "obter_consulta_cnpj",
    "obter_consulta_cnpj_dependencia",
]

logger = logging.getLogger(__name__)


def obter_consulta_cnpj(settings: "Settings | None" = None) -> ConsultaCNPJ | None:
    if settings is not None:
        config = settings
    else:
        from app.core.config import settings as config  # import preguiçoso

    if not config.cnpj_consulta_habilitada or not config.cnpj_consulta_url.strip():
        return None
    return ConsultaBrasilAPI(
        base_url=config.cnpj_consulta_url.strip(), chave=config.cnpj_consulta_chave.strip()
    )


def obter_consulta_cnpj_dependencia() -> ConsultaCNPJ | None:
    """Dependency das rotas de cadastro (os testes trocam por um provedor com
    `httpx.MockTransport`)."""
    return obter_consulta_cnpj()


def consultar_cnpj(cnpj: str, consulta: ConsultaCNPJ | None) -> ResultadoConsultaCNPJ:
    """Situação do CNPJ (já normalizado) na Receita, do cache ou do provedor.
    Nunca levanta."""
    inicio = time.monotonic()
    if consulta is None:
        resultado, origem = INDISPONIVEL, "desligada"
    elif (guardado := cache_por_cnpj.obter(cnpj)) is not None:
        resultado, origem = guardado, "cache"
    else:
        resultado, origem = consulta.consultar(cnpj), "api"
        cache_por_cnpj.guardar(cnpj, resultado)
    logger.info(
        "consulta_cnpj resultado=%s origem=%s duracao_ms=%s",
        resultado.situacao,
        origem,
        round((time.monotonic() - inicio) * 1000),
    )
    return resultado
