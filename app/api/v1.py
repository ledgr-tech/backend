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

Cada requisição sai no log com o id e o prefixo da chave, a rota e o status
(`registrar_acesso`, em app/api/documentacao_publica.py). Nunca o token nem dado de lançamento.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import ChaveAutenticada, obter_chave_api
from app.core.database import get_db
from app.core.rate_limit import LIMITE_API_POR_CHAVE, chave_por_chave_api, limiter
from app.models import Empresa, Fechamento
from app.services import api_publica, decisoes, fechamentos

router = APIRouter(prefix="/v1", tags=["API pública v1"])

PADRAO_COMPETENCIA = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
Competencia = Annotated[
    str,
    Query(
        pattern=PADRAO_COMPETENCIA,
        description="Mês de referência no formato AAAA-MM, por exemplo 2026-09.",
        examples=["2026-09"],
    ),
]
Categoria = Literal[
    "divergente_valor", "divergente_data", "sem_correspondencia", "duplicado", "tarifa_bancaria"
]
Chave = Annotated[ChaveAutenticada, Depends(obter_chave_api)]
Banco = Annotated[Session, Depends(get_db)]


def _limitado(funcao):
    return limiter.limit(LIMITE_API_POR_CHAVE, key_func=chave_por_chave_api)(funcao)


# --- modelos ---------------------------------------------------------------------------


class EmpresaV1(BaseModel):
    """Uma empresa cliente que a chave enxerga."""

    id: uuid.UUID = Field(description="Identificador da empresa, usado nas outras rotas.")
    razao_social: str = Field(description="Razão social da empresa.")
    cnpj: str = Field(description="CNPJ da empresa, sem máscara (14 caracteres).")


class EmpresasV1(BaseModel):
    itens: list[EmpresaV1] = Field(description="As empresas no escopo da chave.")


class ContagensV1(BaseModel):
    """Quantas linhas caíram em cada categoria da conciliação."""

    total: int = Field(description="Total de linhas dos pares conciliados.")
    match_exato: int = Field(description="Linhas que bateram em valor e data.")
    match_tolerancia: int = Field(
        description="Linhas que bateram em valor, com a data dentro da tolerância de dias."
    )
    duplicado: int = Field(description="Lançamentos excedentes de um grupo que já formou par.")
    sem_correspondencia: int = Field(description="Lançamentos sem nada parecido do outro lado.")
    tarifa_bancaria: int = Field(description="Lançamentos do banco reconhecidos como tarifa.")
    divergente_valor: int = Field(
        description="Há lançamento do outro lado na mesma data, com valor diferente."
    )
    divergente_data: int = Field(
        description="Há lançamento do outro lado com o mesmo valor, em outra data."
    )


class ContagensDoMesV1(ContagensV1):
    justificadas: int = Field(description="Divergências com justificativa aprovada.")
    aguardando_aprovacao: int = Field(
        description="Divergências com justificativa ainda não aprovada por admin ou gestor."
    )
    pendentes: int = Field(description="Divergências sem justificativa.")
    linhas_nao_lidas: int = Field(
        description="Linhas dos arquivos de extrato que não puderam ser lidas."
    )


class ResumoV1(BaseModel):
    """A situação de uma competência (mês) de uma empresa."""

    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    fechado: bool = Field(description="O mês está fechado (há um fechamento ativo).")
    fechado_em: datetime | None = Field(description="Quando o mês foi fechado; nulo se aberto.")
    com_ressalva: bool = Field(
        description="O fechamento ativo foi feito com ressalva. O texto da ressalva não é exposto."
    )
    pronto_para_fechar: bool = Field(
        description=(
            "O mês está aberto, tem conciliação e não tem divergência sem justificativa, "
            "justificativa aguardando aprovação nem linha não lida."
        )
    )
    contagens: ContagensDoMesV1


class ContagensDoParV1(ContagensV1):
    justificadas: int = Field(description="Divergências deste par com justificativa aprovada.")
    aguardando_aprovacao: int = Field(
        description="Divergências deste par com justificativa aguardando aprovação."
    )


class ConciliacaoV1(BaseModel):
    """Um par vigente do mês: a rodada mais recente de um extrato do banco."""

    extrato_banco_id: uuid.UUID = Field(description="Extrato do banco do par.")
    extrato_sistema_id: uuid.UUID = Field(description="Extrato do sistema (ERP) do par.")
    rodada: int = Field(
        description="Número da rodada: quantos extratos do sistema já foram conciliados com este extrato do banco."
    )
    executada_em: datetime = Field(description="Quando a conciliação deste par rodou por último.")
    periodo_inicio: date | None = Field(
        description="Primeiro dia com lançamento no extrato do banco."
    )
    periodo_fim: date | None = Field(description="Último dia com lançamento no extrato do banco.")
    contagens: ContagensDoParV1


class ConciliacoesV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    itens: list[ConciliacaoV1]


class DivergenciaV1(BaseModel):
    """Uma linha divergente de um par vigente do mês. Sem descrição do lançamento."""

    extrato_banco_id: uuid.UUID = Field(description="Extrato do banco da linha.")
    chave: str = Field(
        description="Identificador estável da linha dentro do extrato do banco, igual em todas as rodadas."
    )
    categoria: Categoria = Field(description="Tipo de divergência.")
    data: date = Field(description="Data do lançamento (o do banco ou, sem ele, o do sistema).")
    valor: Decimal = Field(
        description="Valor do lançamento, string decimal com duas casas; negativo é saída."
    )
    lado: api_publica.Lado = Field(
        description="De que lado está o lançamento: banco, sistema ou ambos."
    )
    situacao: api_publica.Situacao = Field(
        description=(
            "sem_justificativa: ninguém tratou; aguardando_aprovacao: justificada, falta aprovar; "
            "justificada: justificativa aprovada; conferida: marcada como conferida."
        )
    )


class DivergenciasV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    total: int = Field(description="Total de linhas que atendem aos filtros, em todas as páginas.")
    limit: int
    offset: int
    itens: list[DivergenciaV1]


class FechamentoV1(BaseModel):
    """Um fechamento de mês, ativo ou reaberto. Sem nomes e sem o texto da ressalva."""

    competencia: str = Field(description="Mês fechado, AAAA-MM.")
    fechado_em: datetime = Field(description="Quando o mês foi fechado.")
    com_ressalva: bool = Field(description="Foi fechado com ressalva.")
    reaberto_em: datetime | None = Field(
        description="Quando foi reaberto; nulo se continua fechado."
    )
    contagens: ContagensDoMesV1 = Field(description="As contagens congeladas no fechamento.")


class FechamentosV1(BaseModel):
    itens: list[FechamentoV1]


# --- regras ----------------------------------------------------------------------------


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


def _contagens_do_resumo(resumo: dict) -> ContagensDoMesV1:
    return ContagensDoMesV1(
        **resumo["contagens"],
        justificadas=resumo["justificadas"],
        aguardando_aprovacao=resumo.get("aguardando_aprovacao", 0),
        pendentes=resumo["pendentes"],
        linhas_nao_lidas=resumo["linhas_nao_lidas"],
    )


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
    ativo = fechamentos.fechamento_ativo(db, empresa_id, competencia)
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    resumo = fechamentos.resumo_do_mes(db, empresa_id, pares)
    contagens = _contagens_do_resumo(resumo)
    pronto = (
        ativo is None
        and bool(pares)
        and contagens.pendentes == 0
        and contagens.aguardando_aprovacao == 0
        and contagens.linhas_nao_lidas == 0
    )
    return ResumoV1(
        competencia=competencia,
        fechado=ativo is not None,
        fechado_em=ativo.fechado_em if ativo is not None else None,
        com_ressalva=ativo is not None and ativo.ressalva is not None,
        pronto_para_fechar=pronto,
        contagens=contagens,
    )


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
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    periodos = api_publica.periodos_dos_extratos(
        db, empresa_id, [par.extrato_banco_id for par in pares]
    )
    justificativas = decisoes.contar_justificativas(
        db, empresa_id, {(par.extrato_banco_id, par.extrato_sistema_id) for par in pares}
    )
    itens = []
    for par in pares:
        contagem = justificativas.get((par.extrato_banco_id, par.extrato_sistema_id))
        inicio, fim = periodos.get(par.extrato_banco_id, (None, None))
        itens.append(
            ConciliacaoV1(
                extrato_banco_id=par.extrato_banco_id,
                extrato_sistema_id=par.extrato_sistema_id,
                rodada=par.rodada,
                executada_em=par.execucao.criado_em.replace(tzinfo=UTC),
                periodo_inicio=inicio,
                periodo_fim=fim,
                contagens=ContagensDoParV1(
                    **{c: getattr(par.execucao, c) for c in fechamentos.CATEGORIAS},
                    justificadas=contagem.aprovadas if contagem else 0,
                    aguardando_aprovacao=contagem.aguardando_aprovacao if contagem else 0,
                ),
            )
        )
    return ConciliacoesV1(competencia=competencia, itens=itens)


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
    limit: Annotated[int, Query(ge=1, le=200, description="Itens por página, até 200.")] = 50,
    offset: Annotated[int, Query(ge=0, description="Quantos itens pular.")] = 0,
) -> DivergenciasV1:
    _empresa_no_escopo(db, chave, empresa_id)
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    linhas = [
        d
        for d in api_publica.divergencias_do_mes(db, empresa_id, pares)
        if (categoria is None or d.categoria == categoria)
        and (situacao is None or d.situacao == situacao)
    ]
    return DivergenciasV1(
        competencia=competencia,
        total=len(linhas),
        limit=limit,
        offset=offset,
        itens=[DivergenciaV1(**vars(d)) for d in linhas[offset : offset + limit]],
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
    consulta = select(Fechamento).where(Fechamento.empresa_id == empresa_id)
    if competencia is not None:
        consulta = consulta.where(Fechamento.competencia == competencia)
    itens = db.scalars(
        consulta.order_by(Fechamento.competencia.desc(), Fechamento.fechado_em.desc())
    )
    return FechamentosV1(
        itens=[
            FechamentoV1(
                competencia=f.competencia,
                fechado_em=f.fechado_em,
                com_ressalva=f.ressalva is not None,
                reaberto_em=f.reaberto_em,
                contagens=_contagens_do_resumo(f.resumo),
            )
            for f in itens
        ]
    )
