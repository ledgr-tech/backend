"""Endpoint de upload de extrato (issue #7).

Valida extensão (.ofx/.csv, ver ADR-001) e tamanho (5MB), persiste só os
metadados do extrato — nunca o conteúdo do arquivo (ADR-002) — e retorna
`extrato_id` e `status`. O parsing em si (OFX/CSV) fica pras issues #8/#9;
aqui o status sempre nasce "pendente".

`empresa_id` ainda é recebido explicitamente no form porque a Sprint 1 não
tem autenticação (isso entra na Sprint 2, que troca esse campo pelo valor
resolvido da sessão). Schema já nasce com a coluna obrigatória e indexada
(ADR-004), sem precisar de migração de correção depois.
"""

import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Extrato

router = APIRouter(prefix="/extratos", tags=["extratos"])

FORMATOS_SUPORTADOS = {".ofx", ".csv"}
TAMANHO_MAXIMO_BYTES = 5 * 1024 * 1024  # 5MB — ver issue #7, sem ADR dedicado


class ExtratoUploadResponse(BaseModel):
    extrato_id: uuid.UUID
    status: str


@router.post(
    "/upload",
    response_model=ExtratoUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_extrato(
    empresa_id: Annotated[uuid.UUID, Form()],
    arquivo: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
) -> ExtratoUploadResponse:
    extensao = os.path.splitext(arquivo.filename or "")[1].lower()
    if extensao not in FORMATOS_SUPORTADOS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Formato de arquivo não suportado. Envie um arquivo .ofx ou .csv.",
        )

    conteudo = await arquivo.read()
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Arquivo excede o limite de 5MB.",
        )

    extrato = Extrato(
        empresa_id=empresa_id,
        nome_arquivo=arquivo.filename,
        formato=extensao.lstrip("."),
        tamanho_bytes=len(conteudo),
        status="pendente",
    )
    # conteúdo nunca é gravado em disco nem em coluna — só usado acima pra
    # validar tamanho (ADR-002: arquivo bruto não é persistido)
    del conteudo

    db.add(extrato)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        ) from exc
    db.refresh(extrato)

    return ExtratoUploadResponse(extrato_id=extrato.id, status=extrato.status)
