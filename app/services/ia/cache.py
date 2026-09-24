"""Chave de cache das explicações de divergência (issue #28, ADR-011).

O cache de fato (tabela `explicacoes_divergencia`, leitura/escrita) fica no
endpoint (app/api/explicacoes.py) — este módulo só calcula a chave.
"""

import hashlib
import uuid

from app.services.ia.base import ContextoDivergencia
from app.services.ia.prompt import VERSAO_PROMPT, serializar_contexto


def chave_cache(
    empresa_id: uuid.UUID, provedor: str, modelo: str, contexto: ContextoDivergencia
) -> str:
    """sha256 hex (64 caracteres) de `empresa_id` + `provedor` + `modelo` +
    `VERSAO_PROMPT` + `serializar_contexto(contexto)` (que já mascara e já
    escapa `<`/`>`, ver app/services/ia/prompt.py).

    `provedor` e `modelo` são os da configuração ATIVA (`llm_provedor` e
    `openai_modelo`), nunca o `modelo` que vem no campo `model` da resposta
    da API — a chave não pode depender de nada que o provedor devolva.
    Trocar de provedor/modelo, ou subir `VERSAO_PROMPT`, muda um dos cinco
    componentes e gera uma chave diferente, invalidando o cache sozinho
    (ADR-011). Determinística: a mesma entrada sempre produz a mesma
    chave, e duas empresas com o mesmo contexto geram chaves diferentes
    (o `empresa_id` entra na composição)."""
    bruto = "|".join(
        (str(empresa_id), provedor, modelo, VERSAO_PROMPT, serializar_contexto(contexto))
    )
    return hashlib.sha256(bruto.encode()).hexdigest()
