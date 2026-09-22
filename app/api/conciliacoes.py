"""Endpoints de conciliação: `POST /conciliacoes` (issue #16) roda o motor de
matching exato (app/services/matching.py, ADR-006) pro par de extratos e grava
o resultado em `conciliacoes` (ADR-007). `GET /conciliacoes/{extrato_id}`
(issue #20) consulta o que foi gravado.

É síncrono de propósito (sem BackgroundTasks): o motor exato é só agrupamento
em memória e um insert em lote, e a resposta já traz as contagens.

Exige JWT (app/core/auth.py, ADR-003): o `empresa_id` vem do token. Extrato
inexistente ou de outra empresa devolve 404 igual, sem confirmar a existência
do ID (mitigação de BOLA/IDOR, mesmo padrão de app/api/extratos.py).

Validações, nesta ordem: (a) os dois extratos existem e são da empresa do
token (404); (b) ids diferentes (422); (c) o primeiro tem origem "banco" e o
segundo "sistema" (422); (d) ambos com status "concluido" ou
"concluido_com_erros" (409).

`GET /conciliacoes/{extrato_id}`: o `extrato_id` é o extrato do BANCO, porque a
consulta parte de `extrato_banco_id`, que tem índice (ADR-007). Um extrato do
sistema pode ter sido conciliado com vários extratos de banco, então consultar
por ele seria ambíguo (422). Validações: (a) extrato existe e é da empresa do
token (404, sem confirmar a existência de extrato de outra empresa); (b) origem
diferente de "sistema" (422). Extrato do banco sem nenhuma conciliação devolve
200 com lista vazia. Query params opcionais: `limit` (1 a 1000, default 100),
`offset`, `status` e `extrato_sistema_id` (filtra um par específico). Os itens
saem numa única query com outer join nos dois lançamentos, sempre filtrando por
`empresa_id`, e a ordem é estável pra paginar: data do lançamento (a do banco,
ou a do sistema quando não há do banco), valor e id da conciliação. `valor` e
`score_confianca` são Decimal e vão como string no JSON, pra o cliente não
tratar dinheiro como float.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import obter_empresa_id_autenticada
from app.core.database import get_db
from app.models import Conciliacao, Extrato, Lancamento
from app.services.matching import conciliar_extratos

router = APIRouter(prefix="/conciliacoes", tags=["conciliacoes"])

STATUS_PROCESSADO = {"concluido", "concluido_com_erros"}

StatusConciliacao = Literal[
    "match_exato",
    "match_tolerancia",
    "divergente_valor",
    "divergente_data",
    "sem_correspondencia",
    "duplicado",
    "tarifa_bancaria",
]


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
    tarifa_bancaria: int
    divergente_valor: int
    divergente_data: int


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


class LancamentoResponse(BaseModel):
    id: uuid.UUID
    data: date
    valor: Decimal
    descricao: str
    tipo: str


class ItemConciliacaoResponse(BaseModel):
    id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    status: str
    regra_aplicada: str | None
    score_confianca: Decimal | None
    lancamento_banco: LancamentoResponse | None
    lancamento_sistema: LancamentoResponse | None


class ConciliacaoListaResponse(BaseModel):
    extrato_id: uuid.UUID
    total: int
    limit: int
    offset: int
    itens: list[ItemConciliacaoResponse]


def _lancamento_ou_none(linha, prefixo: str) -> LancamentoResponse | None:
    id_ = getattr(linha, f"{prefixo}_id")
    if id_ is None:
        return None
    return LancamentoResponse(
        id=id_,
        data=getattr(linha, f"{prefixo}_data"),
        valor=getattr(linha, f"{prefixo}_valor"),
        descricao=getattr(linha, f"{prefixo}_descricao"),
        tipo=getattr(linha, f"{prefixo}_tipo"),
    )


@router.get("/{extrato_id}", response_model=ConciliacaoListaResponse)
def listar_conciliacoes(
    extrato_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: StatusConciliacao | None = None,
    extrato_sistema_id: uuid.UUID | None = None,
) -> ConciliacaoListaResponse:
    extrato = _obter_extrato_da_empresa(db, extrato_id, empresa_id)
    if extrato.origem == "sistema":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A consulta deve usar o extrato do banco, não o do sistema.",
        )

    filtros = [
        Conciliacao.empresa_id == empresa_id,
        Conciliacao.extrato_banco_id == extrato_id,
    ]
    if status is not None:
        filtros.append(Conciliacao.status == status)
    if extrato_sistema_id is not None:
        filtros.append(Conciliacao.extrato_sistema_id == extrato_sistema_id)

    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    consulta = (
        select(
            Conciliacao.id,
            Conciliacao.extrato_sistema_id,
            Conciliacao.status,
            Conciliacao.regra_aplicada,
            Conciliacao.score_confianca,
            lanc_banco.id.label("banco_id"),
            lanc_banco.data.label("banco_data"),
            lanc_banco.valor.label("banco_valor"),
            lanc_banco.descricao.label("banco_descricao"),
            lanc_banco.tipo.label("banco_tipo"),
            lanc_sistema.id.label("sistema_id"),
            lanc_sistema.data.label("sistema_data"),
            lanc_sistema.valor.label("sistema_valor"),
            lanc_sistema.descricao.label("sistema_descricao"),
            lanc_sistema.tipo.label("sistema_tipo"),
        )
        .select_from(Conciliacao)
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(*filtros)
        .order_by(
            func.coalesce(lanc_banco.data, lanc_sistema.data),
            func.coalesce(lanc_banco.valor, lanc_sistema.valor),
            Conciliacao.id,
        )
        .limit(limit)
        .offset(offset)
    )
    linhas = db.execute(consulta).all()
    total = db.scalar(select(func.count()).select_from(Conciliacao).where(*filtros))

    itens = [
        ItemConciliacaoResponse(
            id=linha.id,
            extrato_sistema_id=linha.extrato_sistema_id,
            status=linha.status,
            regra_aplicada=linha.regra_aplicada,
            score_confianca=linha.score_confianca,
            lancamento_banco=_lancamento_ou_none(linha, "banco"),
            lancamento_sistema=_lancamento_ou_none(linha, "sistema"),
        )
        for linha in linhas
    ]
    return ConciliacaoListaResponse(
        extrato_id=extrato_id, total=total, limit=limit, offset=offset, itens=itens
    )
