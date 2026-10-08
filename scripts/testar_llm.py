#!/usr/bin/env python3
"""Script manual pra validar a integração com o GPT-6 Luna (issue #28,
ADR-011), com um caso 100% sintético.

GASTA UMA FRAÇÃO DE CENTAVO DE DÓLAR POR EXECUÇÃO (chamada real à API da
OpenAI) — é só pra validação manual, nunca chamado por teste automatizado
nem pelo CI.

Uso:
    OPENAI_API_KEY=sk-... python scripts/testar_llm.py

Sem OPENAI_API_KEY no ambiente, sai com uma mensagem clara e não faz
nenhuma requisição.
"""

import os
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ia.base import (
    CandidatoContexto,
    ContextoDivergencia,
    LancamentoContexto,
    ProvedorIAIndisponivel,
)
from app.services.ia.openai_provider import ProvedorOpenAI
from app.services.ia.prompt import motivo_deterministico


def _contexto_sintetico() -> ContextoDivergencia:
    """Um `divergente_valor` com dois candidatos, descrições inventadas —
    uma delas com CNPJ e e-mail fictícios, pra mostrar o mascaramento em
    ação (app/services/ia/mascaramento.py)."""
    return ContextoDivergencia(
        status="divergente_valor",
        motivo=motivo_deterministico("divergente_valor"),
        lancamento=LancamentoContexto(
            data=date(2026, 9, 20),
            valor=Decimal("1500.00"),
            tipo="credito",
            descricao=(
                "Pagamento fornecedor Silva & Cia, CNPJ 12.345.678/0001-95, "
                "contato financeiro@fornecedor-exemplo.com.br"
            ),
        ),
        candidatos=(
            CandidatoContexto(
                lancamento=LancamentoContexto(
                    data=date(2026, 9, 20),
                    valor=Decimal("1499.00"),
                    tipo="credito",
                    descricao="Recebimento cliente exemplo A",
                ),
                diferenca_valor=Decimal("1.00"),
                diferenca_dias=None,
            ),
            CandidatoContexto(
                lancamento=LancamentoContexto(
                    data=date(2026, 9, 20),
                    valor=Decimal("1600.00"),
                    tipo="credito",
                    descricao="Recebimento cliente exemplo B",
                ),
                diferenca_valor=Decimal("100.00"),
                diferenca_dias=None,
            ),
        ),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
    )


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY não configurada no ambiente — nenhuma requisição foi feita.")
        return

    provedor = ProvedorOpenAI(
        api_key=api_key,
        modelo=os.environ.get("OPENAI_MODELO", "gpt-6-luna"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout_segundos=float(os.environ.get("LLM_TIMEOUT_SEGUNDOS", "20.0")),
        max_tokens_saida=int(os.environ.get("LLM_MAX_TOKENS_SAIDA", "300")),
    )

    inicio = time.monotonic()
    try:
        resposta = provedor.explicar_divergencia(
            _contexto_sintetico(), identificador_anonimo="script-teste-manual"
        )
    except ProvedorIAIndisponivel as exc:
        print(f"Provedor indisponível: motivo={exc.motivo} status_http={exc.status_http}")
        return
    duracao_ms = round((time.monotonic() - inicio) * 1000)

    print(f"Explicação:\n{resposta.texto}\n")
    print(f"Modelo: {resposta.modelo}")
    print(f"Tokens: entrada={resposta.tokens_entrada} saída={resposta.tokens_saida}")
    print(f"Duração: {duracao_ms}ms")


if __name__ == "__main__":
    main()
