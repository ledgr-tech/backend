"""`GET /execucoes` (issue #27, ADR-010): histórico de execuções de
conciliação por empresa — data, contagens e percentual de acerto de cada
rodada de `POST /conciliacoes` (app/api/conciliacoes.py), não só a última.

Exige JWT (app/core/auth.py, ADR-003): sempre filtra por `empresa_id` do
token, nunca recebe esse id por parâmetro. Query params: `limit` (1 a 100,
default 20), `offset` (>= 0) e `extrato_banco_id` (opcional, issue #82).
Ordem: `criado_em` desc, `id` desc (mais recente primeiro, desempate
estável pra paginar). Empresa sem nenhuma execução devolve 200 com lista
vazia.

`atual` é verdadeiro só na execução mais recente de cada par (empresa_id,
extrato_banco_id, extrato_sistema_id), calculado com `row_number()` numa
window function particionada pelo par — nunca com uma query por item (N+1):
`conciliacoes` (ADR-007) guarda só a última rodada de cada par, então o
drill-down (`GET /conciliacoes/{extrato_id}`) de uma execução antiga mostra
o estado atual do par, não o daquela rodada, e este campo sinaliza isso pro
frontend. Os nomes de arquivo vêm de join com `extratos`.

`extrato_banco_id` (issue #82) filtra pra achar as rodadas de uma
conciliação sem varrer páginas (antes disso, o frontend lia até 3 páginas
de 50 execuções). UUID inválido dá 422 (comportamento padrão do FastAPI
pro tipo `uuid.UUID`); extrato inexistente ou de outra empresa dá 404, com
a mesma mensagem de `GET /extratos/{id}`. O filtro entra no WHERE da
subconsulta, antes do `row_number()`: como a window function já particiona
por `extrato_banco_id`, filtrar ali não muda quais linhas são "atual" dentro
do par, só remove os outros extratos do banco da consulta. `total` passa a
contar só as execuções filtradas.

`executada_em` sai em UTC explícito (sufixo `Z`/`+00:00` no JSON): a coluna
`criado_em` é `TIMESTAMP` sem timezone e o `now()` do Postgres está em UTC
(confirmado com `SHOW timezone`), mas sem o sufixo o navegador do frontend
interpretaria a string como horário local, adiantando/atrasando a hora
mostrada. `linha.criado_em` vem "naive" do driver; marcamos `tzinfo=UTC`
explicitamente antes de devolver, sem tocar no schema do banco.

`periodo_inicio`/`periodo_fim` (issue #82) são o período do EXTRATO DO
BANCO daquela execução (decisão de 05/10, não o período do par): a menor e
a maior data dos lançamentos daquele extrato, gravadas por
`app/services/normalizacao.py`. Vêm sempre na resposta, com `null` quando o
extrato do banco não tem período (ainda pendente/processando, ou sem
lançamento válido).

`contagens.justificadas` (issue #83): nas execuções com `atual: true`, o
número de linhas DIVERGENTES do par em `conciliacoes` cuja decisão em vigor é
`justificada` — de qualquer rodada, porque a justificativa sobrevive à
rodada nova (regras em app/services/decisoes.py). Nas execuções com
`atual: false` é 0 (decisão de 05/10): `conciliacoes` só tem a última rodada
de cada par, então não há como saber o que estava justificado naquela época.
Fica FORA de `total`, que continua a soma das sete categorias (não é coluna,
o CHECK da tabela não muda). Custo: duas consultas a mais por página, não
por item — as linhas divergentes dos pares atuais da página e os eventos dos
extratos do banco envolvidos (`contar_justificadas`).

Nunca loga descrição, valor nem nome de arquivo (mesma regra de
app/services/matching.py).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import obter_empresa_id_autenticada
from app.core.database import get_db
from app.models import ExecucaoConciliacao, Extrato
from app.services.decisoes import contar_justificadas
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
    justificadas: int


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
    periodo_inicio: date | None
    periodo_fim: date | None


class ExecucaoListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[ItemExecucaoResponse]


def _extrato_nao_encontrado() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND,
        detail="Extrato não encontrado.",
    )


@router.get("", response_model=ExecucaoListaResponse)
def listar_execucoes(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    extrato_banco_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ExecucaoListaResponse:
    if extrato_banco_id is not None:
        extrato = db.get(Extrato, extrato_banco_id)
        if extrato is None or extrato.empresa_id != empresa_id:
            raise _extrato_nao_encontrado()

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

    filtros = [ExecucaoConciliacao.empresa_id == empresa_id]
    if extrato_banco_id is not None:
        filtros.append(ExecucaoConciliacao.extrato_banco_id == extrato_banco_id)

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
            extrato_banco.periodo_inicio.label("periodo_inicio"),
            extrato_banco.periodo_fim.label("periodo_fim"),
            rn,
        )
        .select_from(ExecucaoConciliacao)
        .join(extrato_banco, ExecucaoConciliacao.extrato_banco_id == extrato_banco.id)
        .join(extrato_sistema, ExecucaoConciliacao.extrato_sistema_id == extrato_sistema.id)
        .where(*filtros)
        .subquery()
    )

    consulta = (
        select(subconsulta)
        .order_by(subconsulta.c.criado_em.desc(), subconsulta.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    linhas = db.execute(consulta).all()
    total = db.scalar(select(func.count()).select_from(ExecucaoConciliacao).where(*filtros))
    justificadas = contar_justificadas(
        db,
        empresa_id,
        {(linha.extrato_banco_id, linha.extrato_sistema_id) for linha in linhas if linha.rn == 1},
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
                **{categoria: getattr(linha, categoria) for categoria in CATEGORIAS_CONTAGEM},
                justificadas=(
                    justificadas.get((linha.extrato_banco_id, linha.extrato_sistema_id), 0)
                    if linha.rn == 1
                    else 0
                ),
            ),
            percentual_acerto=calcular_percentual_acerto(
                linha.match_exato, linha.match_tolerancia, linha.total
            ),
            atual=(linha.rn == 1),
            periodo_inicio=linha.periodo_inicio,
            periodo_fim=linha.periodo_fim,
        )
        for linha in linhas
    ]
    return ExecucaoListaResponse(total=total, limit=limit, offset=offset, itens=itens)
