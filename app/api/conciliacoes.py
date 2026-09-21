"""Endpoint de conciliação (issue #16): `POST /conciliacoes` roda o motor de
matching exato (app/services/matching.py, ADR-006) pro par de extratos e grava
o resultado em `conciliacoes` (ADR-007).

É síncrono de propósito (sem BackgroundTasks): o motor exato é só agrupamento
em memória e um insert em lote, e a resposta já traz as contagens.

Exige JWT (app/core/auth.py, ADR-003): o `empresa_id` vem do token. Extrato
inexistente ou de outra empresa devolve 404 igual, sem confirmar a existência
do ID (mitigação de BOLA/IDOR, mesmo padrão de app/api/extratos.py).

Validações, nesta ordem: (a) os dois extratos existem e são da empresa do
token (404); (b) ids diferentes (422); (c) o primeiro tem origem "banco" e o
segundo "sistema" (422); (d) ambos com status "concluido" ou
"concluido_com_erros" (409).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import obter_empresa_id_autenticada
from app.core.database import get_db
from app.models import Extrato
from app.services.matching import conciliar_extratos

router = APIRouter(prefix="/conciliacoes", tags=["conciliacoes"])

STATUS_PROCESSADO = {"concluido", "concluido_com_erros"}


class ConciliacaoRequest(BaseModel):
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID


class ConciliacaoResponse(BaseModel):
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    total: int
    match_exato: int
    duplicado: int
    sem_correspondencia: int


def _obter_extrato_da_empresa(db: Session, extrato_id: uuid.UUID, empresa_id: uuid.UUID) -> Extrato:
    extrato = db.get(Extrato, extrato_id)
    if extrato is None or extrato.empresa_id != empresa_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Extrato não encontrado.",
        )
    return extrato


@router.post(
    "",
    response_model=ConciliacaoResponse,
    status_code=http_status.HTTP_201_CREATED,
)
def criar_conciliacao(
    corpo: ConciliacaoRequest,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
) -> ConciliacaoResponse:
    extrato_banco = _obter_extrato_da_empresa(db, corpo.extrato_banco_id, empresa_id)
    extrato_sistema = _obter_extrato_da_empresa(db, corpo.extrato_sistema_id, empresa_id)

    if corpo.extrato_banco_id == corpo.extrato_sistema_id:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Os extratos do banco e do sistema devem ser diferentes.",
        )

    if extrato_banco.origem != "banco" or extrato_sistema.origem != "sistema":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="O primeiro extrato deve ter origem 'banco' e o segundo, origem 'sistema'.",
        )

    if (
        extrato_banco.status not in STATUS_PROCESSADO
        or extrato_sistema.status not in STATUS_PROCESSADO
    ):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Um dos extratos ainda não terminou de processar ou falhou.",
        )

    contagens = conciliar_extratos(db, empresa_id, corpo.extrato_banco_id, corpo.extrato_sistema_id)
    return ConciliacaoResponse(
        extrato_banco_id=corpo.extrato_banco_id,
        extrato_sistema_id=corpo.extrato_sistema_id,
        **contagens,
    )
