"""Fábrica de provedor de IA e reexport dos tipos públicos (issue #28,
ADR-011).

`obter_provedor` decide, a partir da configuração, se e qual provedor
instanciar. Hoje só existe "openai" (GPT-6 Luna) — o Sabiazinho entra aqui
numa issue própria, atrás da mesma interface `ProvedorDeIA`.
"""

from app.core.config import Settings
from app.core.config import settings as _settings_padrao
from app.services.ia.base import (
    CandidatoContexto,
    ContextoDivergencia,
    LancamentoContexto,
    ProvedorDeIA,
    ProvedorIAIndisponivel,
    RespostaIA,
)
from app.services.ia.openai_provider import ProvedorOpenAI

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


def obter_provedor(settings: Settings | None = None) -> ProvedorDeIA | None:
    """Nenhum provedor (`None`) quando `llm_habilitado` é falso, a chave
    configurada está vazia, ou `llm_provedor` não é reconhecido — só
    "openai" existe hoje."""
    config = settings if settings is not None else _settings_padrao
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
