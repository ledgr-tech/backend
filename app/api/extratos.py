"""Endpoints de extrato: upload (issue #7) e relatório de validação (issue #13).

`POST /extratos/upload` valida extensão (.ofx/.csv, ver ADR-001) e tamanho
(5MB), persiste só os metadados do extrato — nunca o conteúdo do arquivo
(ADR-002) — e retorna `extrato_id` e `status="pendente"` imediatamente, sem
esperar o parsing.

Desde a issue #12, o conteúdo lido em memória é passado pra BackgroundTask
de normalização (`app.services.normalizacao.normalizar_extrato`), agendada
depois do commit do Extrato — ela quem parseia (OFX/CSV, issues #8/#9) e
grava os lançamentos, atualizando o status pra "processando"/"concluido"/
"concluido_com_erros"/"erro" (issue #13). O conteúdo continua nunca sendo
persistido (ADR-002): só existe em memória até a task terminar de
processar e descartar.

`GET /extratos/{extrato_id}` (issue #13) devolve o relatório de validação:
status atual, quantidade de lançamentos válidos e a lista de linhas/
transações que não normalizaram (identificador + motivo), pra quem chamou
o upload acompanhar o resultado do processamento assíncrono.

`empresa_id` ainda é recebido explicitamente no form porque a Sprint 1 não
tem autenticação (isso entra na Sprint 2, que troca esse campo pelo valor
resolvido da sessão). Schema já nasce com a coluna obrigatória e indexada
(ADR-004), sem precisar de migração de correção depois. O GET novo também
não tem autenticação ainda — mesma situação do POST hoje, protegê-los é
escopo da issue #14.
"""

import os
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Extrato
from app.services.normalizacao import normalizar_extrato

router = APIRouter(prefix="/extratos", tags=["extratos"])

FORMATOS_SUPORTADOS = {".ofx", ".csv"}
TAMANHO_MAXIMO_BYTES = 5 * 1024 * 1024  # 5MB — ver issue #7, sem ADR dedicado


class ExtratoUploadResponse(BaseModel):
    extrato_id: uuid.UUID
    status: str


class ErroLinhaResponse(BaseModel):
    identificador: str
    motivo: str


class ExtratoDetalheResponse(BaseModel):
    extrato_id: uuid.UUID
    status: str
    quantidade_lancamentos: int | None
    erros: list[ErroLinhaResponse]


@router.post(
    "/upload",
    response_model=ExtratoUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_extrato(
    empresa_id: Annotated[uuid.UUID, Form()],
    arquivo: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
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

    # Conteúdo nunca é gravado em disco nem em coluna (ADR-002) — só passa
    # em memória pra task de normalização, que descarta depois de processar.
    background_tasks.add_task(normalizar_extrato, extrato.id, extrato.formato, conteudo)

    return ExtratoUploadResponse(extrato_id=extrato.id, status=extrato.status)


@router.get("/{extrato_id}", response_model=ExtratoDetalheResponse)
async def obter_extrato(
    extrato_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> ExtratoDetalheResponse:
    extrato = db.get(Extrato, extrato_id)
    if extrato is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Extrato não encontrado.",
        )

    erros = [
        ErroLinhaResponse(identificador=linha.identificador, motivo=linha.motivo)
        for linha in extrato.linhas_invalidas
    ]

    return ExtratoDetalheResponse(
        extrato_id=extrato.id,
        status=extrato.status,
        quantidade_lancamentos=extrato.quantidade_lancamentos,
        erros=erros,
    )
