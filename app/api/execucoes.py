"""`GET /execucoes` (issue #27, ADR-010): histórico de execuções de
conciliação por empresa — data, contagens e percentual de acerto de cada
rodada de `POST /conciliacoes` (app/api/conciliacoes.py), não só a última.

Exige JWT (app/core/auth.py, ADR-003): sempre filtra por `empresa_id` do
token, nunca recebe esse id por parâmetro. Query params: `limit` (1 a 100,
default 20) e `offset` (>= 0). Ordem: `criado_em` desc, `id` desc (mais
recente primeiro, desempate estável pra paginar). Empresa sem nenhuma
execução devolve 200 com lista vazia.

`atual` é verdadeiro só na execução mais recente de cada par (empresa_id,
extrato_banco_id, extrato_sistema_id), calculado com `row_number()` numa
window function particionada pelo par — nunca com uma query por item (N+1):
`conciliacoes` (ADR-007) guarda só a última rodada de cada par, então o
drill-down (`GET /conciliacoes/{extrato_id}`) de uma execução antiga mostra
o estado atual do par, não o daquela rodada, e este campo sinaliza isso pro
frontend. Os nomes de arquivo vêm de join com `extratos`.

`executada_em` sai em UTC explícito (sufixo `Z`/`+00:00` no JSON): a coluna
`criado_em` é `TIMESTAMP` sem timezone e o `now()` do Postgres está em UTC
(confirmado com `SHOW timezone`), mas sem o sufixo o navegador do frontend
interpretaria a string como horário local, adiantando/atrasando a hora
mostrada. `linha.criado_em` vem "naive" do driver; marcamos `tzinfo=UTC`
explicitamente antes de devolver, sem tocar no schema do banco.

Nunca loga descrição, valor nem nome de arquivo (mesma regra de
app/services/matching.py).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import obter_empresa_id_autenticada
from app.core.database import get_db
from app.models import ExecucaoConciliacao, Extrato
from app.services.execucoes import calcular_percentual_acerto

router = APIRouter(prefix="/execucoes", tags=["execucoes"])

CATEGORIAS_CONTAGEM = (
    "total",
    "match_exato",
    "match_tolerancia",
    "duplicado",
    "sem_correspondencia",
    "tarifa_bancaria",
    "divergente_valor",
    "divergente_data",
)


class ContagensResponse(BaseModel):
    total: int
    match_exato: int
    match_tolerancia: int
    duplicado: int
    sem_correspondencia: int
    tarifa_bancaria: int
    divergente_valor: int
    divergente_data: int


class ItemExecucaoResponse(BaseModel):
    id: uuid.UUID
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    nome_arquivo_banco: str
    nome_arquivo_sistema: str
    executada_em: datetime
    tolerancia_dias: int
    contagens: ContagensResponse
    percentual_acerto: Decimal | None
    atual: bool


class ExecucaoListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[ItemExecucaoResponse]


@router.get("", response_model=ExecucaoListaResponse)
def listar_execucoes(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExecucaoListaResponse:
    extrato_banco = aliased(Extrato)
    extrato_sistema = aliased(Extrato)

    rn = (
        func.row_number()
        .over(
            partition_by=(
                ExecucaoConciliacao.empresa_id,
                ExecucaoConciliacao.extrato_banco_id,
                ExecucaoConciliacao.extrato_sistema_id,
            ),
            order_by=(ExecucaoConciliacao.criado_em.desc(), ExecucaoConciliacao.id.desc()),
        )
        .label("rn")
    )

    subconsulta = (
        select(
            ExecucaoConciliacao.id,
            ExecucaoConciliacao.extrato_banco_id,
            ExecucaoConciliacao.extrato_sistema_id,
            ExecucaoConciliacao.criado_em,
            ExecucaoConciliacao.tolerancia_dias,
            *(getattr(ExecucaoConciliacao, categoria) for categoria in CATEGORIAS_CONTAGEM),
            extrato_banco.nome_arquivo.label("nome_arquivo_banco"),
            extrato_sistema.nome_arquivo.label("nome_arquivo_sistema"),
            rn,
        )
        .select_from(ExecucaoConciliacao)
        .join(extrato_banco, ExecucaoConciliacao.extrato_banco_id == extrato_banco.id)
        .join(extrato_sistema, ExecucaoConciliacao.extrato_sistema_id == extrato_sistema.id)
        .where(ExecucaoConciliacao.empresa_id == empresa_id)
        .subquery()
    )

    consulta = (
        select(subconsulta)
        .order_by(subconsulta.c.criado_em.desc(), subconsulta.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    linhas = db.execute(consulta).all()
    total = db.scalar(
        select(func.count())
        .select_from(ExecucaoConciliacao)
        .where(ExecucaoConciliacao.empresa_id == empresa_id)
    )

    itens = [
        ItemExecucaoResponse(
            id=linha.id,
            extrato_banco_id=linha.extrato_banco_id,
            extrato_sistema_id=linha.extrato_sistema_id,
            nome_arquivo_banco=linha.nome_arquivo_banco,
            nome_arquivo_sistema=linha.nome_arquivo_sistema,
            executada_em=linha.criado_em.replace(tzinfo=UTC),
            tolerancia_dias=linha.tolerancia_dias,
            contagens=ContagensResponse(
                **{categoria: getattr(linha, categoria) for categoria in CATEGORIAS_CONTAGEM}
            ),
            percentual_acerto=calcular_percentual_acerto(
                linha.match_exato, linha.match_tolerancia, linha.total
            ),
            atual=(linha.rn == 1),
        )
        for linha in linhas
    ]
    return ExecucaoListaResponse(total=total, limit=limit, offset=offset, itens=itens)
