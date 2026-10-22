"""API pública do Ledgr, versão 1 (issue #135, ADR-021).

Só leitura, com prefixo próprio (`/v1`) e contrato estável, separado das rotas
do app. Autenticação pela chave de API (`Authorization: Bearer ledgr_...`,
`obter_chave_api` em app/core/auth.py); o JWT do app não vale aqui. A empresa
vem na URL e precisa estar no escopo da chave: fora dele, ou inexistente, 404
"Empresa não encontrada.", com a mesma mensagem. Rate limit de
`LIMITE_API_POR_CHAVE` por chave, com 429 e `Retry-After`.

O que a API nunca devolve: descrição de lançamento, texto de justificativa,
texto da ressalva, nome de arquivo e nome de pessoa. Ela é feita para
ferramentas de terceiros e para o MCP (#136), que levam o que leem para fora
do Ledgr. Sem CORS: é de servidor para servidor, e a chave nunca deve ir para
o navegador.

Datas em ISO 8601, valores em string decimal com duas casas, como no resto da
API. `competencia` é "AAAA-MM", com mês de 01 a 12 (fora disso, 422).

Os modelos e o cálculo moram em app/services/api_publica.py, a regra única
com as ferramentas do MCP (issue #136): aqui ficam o escopo da chave e o
contrato HTTP.

Cada requisição sai no log com o id e o prefixo da chave, a rota e o status
(`registrar_acesso`, em app/api/documentacao_publica.py). Nunca o token nem dado de lançamento.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import ChaveAutenticada, obter_chave_api
from app.core.database import get_db
from app.core.rate_limit import LIMITE_API_POR_CHAVE, chave_por_chave_api, limiter
from app.models import Empresa
from app.services import api_publica
from app.services.api_publica import (
    PADRAO_COMPETENCIA,
    Categoria,
    ConciliacoesV1,
    DivergenciasV1,
    EmpresasV1,
    EmpresaV1,
    FechamentosV1,
    ResumoV1,
)

router = APIRouter(prefix="/v1", tags=["API pública v1"])

Competencia = Annotated[
    str,
    Query(
        pattern=PADRAO_COMPETENCIA,
        description="Mês de referência no formato AAAA-MM, por exemplo 2026-09.",
        examples=["2026-09"],
    ),
]
Chave = Annotated[ChaveAutenticada, Depends(obter_chave_api)]
Banco = Annotated[Session, Depends(get_db)]


def _limitado(funcao):
    return limiter.limit(LIMITE_API_POR_CHAVE, key_func=chave_por_chave_api)(funcao)


# --- escopo -----------------------------------------------------------------------------


def _empresa_no_escopo(db: Session, chave: ChaveAutenticada, empresa_id: uuid.UUID) -> Empresa:
    empresa = db.get(Empresa, empresa_id)
    if (
        empresa is None
        or empresa.conta_id != chave.conta_id
        or (chave.empresa_id is not None and chave.empresa_id != empresa_id)
    ):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    return empresa


# --- rotas -----------------------------------------------------------------------------


@router.get(
    "/empresas",
    response_model=EmpresasV1,
    summary="Empresas que a chave enxerga",
    description=(
        "Lista as empresas clientes no escopo da chave: todas as da conta, se a chave é da "
        "conta, ou só uma, se a chave é de uma empresa. Use o id nas outras rotas."
    ),
)
@_limitado
def listar_empresas(request: Request, chave: Chave, db: Banco) -> EmpresasV1:
    consulta = select(Empresa).where(Empresa.conta_id == chave.conta_id)
    if chave.empresa_id is not None:
        consulta = consulta.where(Empresa.id == chave.empresa_id)
    empresas = db.scalars(consulta.order_by(Empresa.razao_social, Empresa.id))
    return EmpresasV1(
        itens=[EmpresaV1(id=e.id, razao_social=e.razao_social, cnpj=e.cnpj) for e in empresas]
    )


@router.get(
    "/empresas/{empresa_id}/resumo",
    response_model=ResumoV1,
    summary="Situação do mês",
    description=(
        "Diz se o mês está fechado, se está pronto para fechar e quantas linhas há em cada "
        "categoria e situação, com a mesma regra do fechamento do Ledgr: para cada extrato do "
        "banco do mês, vale a conciliação mais recente."
    ),
)
@_limitado
def resumo_do_mes(
    request: Request, empresa_id: uuid.UUID, competencia: Competencia, chave: Chave, db: Banco
) -> ResumoV1:
    _empresa_no_escopo(db, chave, empresa_id)
    return api_publica.resumo_do_mes(db, empresa_id, competencia)


@router.get(
    "/empresas/{empresa_id}/conciliacoes",
    response_model=ConciliacoesV1,
    summary="Conciliações vigentes do mês",
    description=(
        "Os pares vigentes do mês: para cada extrato do banco com período começando no mês, a "
        "conciliação da rodada mais recente, com as contagens por categoria."
    ),
)
@_limitado
def conciliacoes_do_mes(
    request: Request, empresa_id: uuid.UUID, competencia: Competencia, chave: Chave, db: Banco
) -> ConciliacoesV1:
    _empresa_no_escopo(db, chave, empresa_id)
    return api_publica.conciliacoes_do_mes(db, empresa_id, competencia)


@router.get(
    "/empresas/{empresa_id}/divergencias",
    response_model=DivergenciasV1,
    summary="Divergências do mês",
    description=(
        "As linhas divergentes dos pares vigentes do mês, com a categoria, a data, o valor, o "
        "lado e a situação de cada uma. Nunca traz a descrição do lançamento nem o texto da "
        "justificativa. Paginada por limit e offset, em ordem de data e valor."
    ),
)
@_limitado
def divergencias_do_mes(
    request: Request,
    empresa_id: uuid.UUID,
    competencia: Competencia,
    chave: Chave,
    db: Banco,
    categoria: Annotated[
        Categoria | None, Query(description="Filtra por tipo de divergência.")
    ] = None,
    situacao: Annotated[
        api_publica.Situacao | None, Query(description="Filtra pela situação da linha.")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=api_publica.LIMITE_MAXIMO, description="Itens por página, até 200.")
    ] = api_publica.LIMITE_PADRAO,
    offset: Annotated[int, Query(ge=0, description="Quantos itens pular.")] = 0,
) -> DivergenciasV1:
    _empresa_no_escopo(db, chave, empresa_id)
    return api_publica.divergencias_do_mes(
        db, empresa_id, competencia, categoria, situacao, limit, offset
    )


@router.get(
    "/empresas/{empresa_id}/fechamentos",
    response_model=FechamentosV1,
    summary="Fechamentos de mês",
    description=(
        "Os fechamentos da empresa, ativos e reabertos, dos mais recentes para os mais antigos, "
        "com as contagens congeladas no momento do fechamento. Sem nomes e sem o texto da "
        "ressalva."
    ),
)
@_limitado
def listar_fechamentos(
    request: Request,
    empresa_id: uuid.UUID,
    chave: Chave,
    db: Banco,
    competencia: Annotated[
        str | None,
        Query(
            pattern=PADRAO_COMPETENCIA,
            description="Mês de referência AAAA-MM; sem ele, todos os meses.",
        ),
    ] = None,
) -> FechamentosV1:
    _empresa_no_escopo(db, chave, empresa_id)
    return api_publica.fechamentos_da_empresa(db, empresa_id, competencia)
