"""Fábrica de provedor de IA e reexport dos tipos públicos (issue #28,
ADR-011).

`obter_provedor` decide, a partir da configuração, se e qual provedor
instanciar. Hoje só existe "openai" (GPT-6 Luna) — o Sabiazinho entra aqui
numa issue própria, atrás da mesma interface `ProvedorDeIA`.

Import de `settings` é preguiçoso, dentro de `obter_provedor` — importar
este pacote (ou o script manual, scripts/testar_llm.py) não deve exigir
`DATABASE_URL` nem `.env`, já que nada aqui toca banco. `Settings` só entra
como tipo, sob `TYPE_CHECKING`, sem custo de import em tempo de execução.
"""

from typing import TYPE_CHECKING

from app.services.ia.base import (
    CandidatoContexto,
    ContextoDivergencia,
    LancamentoContexto,
    ProvedorDeIA,
    ProvedorIAIndisponivel,
    RespostaIA,
)
from app.services.ia.openai_provider import ProvedorOpenAI

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "CandidatoContexto",
    "ContextoDivergencia",
    "LancamentoContexto",
    "ProvedorDeIA",
    "ProvedorIAIndisponivel",
    "ProvedorOpenAI",
    "RespostaIA",
    "obter_provedor",
]


def obter_provedor(settings: "Settings | None" = None) -> ProvedorDeIA | None:
    """Nenhum provedor (`None`) quando `llm_habilitado` é falso, a chave
    configurada está vazia, ou `llm_provedor` não é reconhecido — só
    "openai" existe hoje."""
    if settings is not None:
        config = settings
    else:
        from app.core.config import settings as config  # import preguiçoso

    if not config.llm_habilitado:
        return None
    if config.llm_provedor != "openai":
        return None
    if not config.openai_api_key.strip():
        return None
    return ProvedorOpenAI(
        api_key=config.openai_api_key,
        modelo=config.openai_modelo,
        base_url=config.openai_base_url,
        timeout_segundos=config.llm_timeout_segundos,
        max_tokens_saida=config.llm_max_tokens_saida,
    )
