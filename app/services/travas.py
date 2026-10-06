"""Travas de escrita (issue #86, ADR-014): `pg_advisory_xact_lock`, soltas
sozinhas no commit ou rollback. Único lugar com as chaves.

- Trava do MÊS, por `(empresa_id, competencia)`. Fechar e reabrir pegam a
  exclusiva; conciliar e decidir pegam a compartilhada, então não se bloqueiam
  entre si e só esperam um fechamento ou reabertura em andamento.
- Trava do EXTRATO DO BANCO, por `(empresa_id, extrato_banco_id)`. Pega
  `conciliar_extratos` e o POST de decisões. Substitui a antiga trava por par
  (banco, sistema): com ela, uma decisão e uma rodada nova com OUTRO extrato do
  sistema não se esperavam, e a decisão podia ser gravada com a rodada anterior.

Ordem fixa, para não haver deadlock: primeiro a trava do mês (se houver
competência), depois a do extrato do banco. Fechar e reabrir pegam só a do mês.
As travas de transação são reentrantes na mesma sessão, então pegar a mesma
duas vezes (rota e depois `conciliar_extratos`) não trava.

Cada chave tem um prefixo próprio no texto hasheado ("mes|", "banco|") para
não colidir com as outras.
"""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Extrato


def _chave(prefixo: str, *partes: object) -> int:
    """Inteiro de 64 bits com sinal (primeiros 8 bytes de um sha256)."""
    bruto = "|".join([prefixo, *(str(parte) for parte in partes)]).encode()
    return int.from_bytes(hashlib.sha256(bruto).digest()[:8], "big", signed=True)


def chave_do_mes(empresa_id: uuid.UUID, competencia: str) -> int:
    return _chave("mes", empresa_id, competencia)


def chave_do_extrato_banco(empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID) -> int:
    return _chave("banco", empresa_id, extrato_banco_id)


def competencia_do_extrato(extrato: Extrato) -> str | None:
    """Mês (AAAA-MM) de `periodo_inicio`; extrato sem período não tem
    competência. Extrato que cruza dois meses fica no mês em que começa."""
    if extrato.periodo_inicio is None:
        return None
    return extrato.periodo_inicio.strftime("%Y-%m")


def travar_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> None:
    """Exclusiva: fechar e reabrir."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:chave)"),
        {"chave": chave_do_mes(empresa_id, competencia)},
    )


def travar_mes_compartilhada(db: Session, empresa_id: uuid.UUID, competencia: str) -> None:
    """Compartilhada: conciliar e decidir."""
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:chave)"),
        {"chave": chave_do_mes(empresa_id, competencia)},
    )


def travar_extrato_banco(db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:chave)"),
        {"chave": chave_do_extrato_banco(empresa_id, extrato_banco_id)},
    )
