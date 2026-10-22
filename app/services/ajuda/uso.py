"""Limites diários e perguntas sem resposta do assistente (issue #137, ADR-020).

Os limites são separados dos das explicações (#140): `ASSISTENTE_LIMITE_DIARIO_
CONTA` por conta e `ASSISTENTE_LIMITE_DIARIO_GLOBAL` no total, contados em
`usos_assistente` desde a meia-noite UTC. Só a pergunta respondida pelo
provedor conta: falha do provedor não grava uso. A contagem não trava nada,
então duas perguntas simultâneas da mesma conta no limite podem passar por
uma; é aceitável.

`perguntas_sem_resposta` guarda só a pergunta já mascarada, a tela e o
instante. As duas tabelas apagam o que tem mais de `RETENCAO` a cada gravação.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import PerguntaSemResposta, UsoAssistente

RETENCAO = timedelta(days=90)


def _inicio_do_dia_utc() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def limite_atingido(db: Session, conta_id: uuid.UUID) -> Literal["conta", "global"] | None:
    hoje = _inicio_do_dia_utc()
    da_conta = db.scalar(
        select(func.count()).where(
            UsoAssistente.conta_id == conta_id, UsoAssistente.criado_em >= hoje
        )
    )
    if da_conta >= settings.assistente_limite_diario_conta:
        return "conta"
    total = db.scalar(select(func.count()).where(UsoAssistente.criado_em >= hoje))
    if total >= settings.assistente_limite_diario_global:
        return "global"
    return None


def registrar_uso(db: Session, conta_id: uuid.UUID) -> None:
    """Grava o uso e apaga os de mais de 90 dias, sem commit."""
    db.execute(delete(UsoAssistente).where(UsoAssistente.criado_em < func.now() - RETENCAO))
    db.add(UsoAssistente(conta_id=conta_id))


def registrar_sem_resposta(db: Session, pergunta_mascarada: str, tela: str | None) -> None:
    """Grava a pergunta já mascarada e apaga as de mais de 90 dias, sem commit."""
    db.execute(
        delete(PerguntaSemResposta).where(PerguntaSemResposta.criado_em < func.now() - RETENCAO)
    )
    db.add(PerguntaSemResposta(pergunta=pergunta_mascarada, tela=tela))
