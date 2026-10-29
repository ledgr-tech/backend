"""Empresas clientes da conta (issue #133, ADR-019).

Rotas da conta: ignoram o cabeçalho `X-Ledgr-Empresa`. São elas que dizem
quais empresas o front pode mandar nele.

`GET /empresas` lista as empresas da conta, para todos os papéis, paginada
desde a issue #157, com o envelope das outras listagens (`total`, `limit`,
`offset`, `itens`). Parâmetros:

- `busca`: pela razão social, sem diferenciar maiúsculas nem acento, ou pelo
  CNPJ, com ou sem máscara. O CNPJ só entra na busca quando o texto, sem a
  máscara, tem algum dígito: senão "Padaria" acharia um CNPJ alfanumérico com
  essas letras. A comparação sem acento é um `translate` dos acentos do
  português (`_COM_ACENTO`), sem a extensão `unaccent`;
- `ordem`: `nome` (padrão), `cnpj` ou `status`, e `direcao`: `asc` (padrão) ou
  `desc`. As favoritas do usuário vêm sempre primeiro, em qualquer ordem; a
  pedida vale dentro de cada grupo. Desempate pela razão social e pelo id;
- `limit` de 1 a 100 (padrão 20) e `offset` a partir de 0. `offset` além do
  total dá 200 com a lista vazia e o total, como nas outras listagens.
  `ordem` e `direcao` fora da lista, `limit` e `offset` fora do intervalo dão
  422.

Cada item traz `favorita` e `status`, a situação do mês mais recente da
empresa. O mês é a competência (mês de `periodo_inicio`) mais recente entre
os extratos do banco que já entraram numa conciliação, a regra do fechamento
(app/services/fechamentos.py): `fechado` quando esse mês tem fechamento
ativo, `em_aberto` quando não, e `sem_conciliacao` quando a empresa nunca
conciliou (ou só extratos sem período). Tudo numa consulta por página: o
status sai de uma subconsulta agrupada por empresa, o favorito de um outer
join e o total de `count(*) OVER ()`. A página além do total não traz linha
de onde ler o total, e só ela faz uma segunda consulta, a contagem. Sem índice novo para a busca: a
consulta parte do índice de `conta_id`, e uma conta tem poucas empresas.

`PUT` e `DELETE /empresas/{empresa_id}/favorita` (issue #157) favoritam e
desfavoritam para o usuário do token, não para a conta. Idempotentes, 204,
qualquer papel. Empresa de outra conta ou inexistente dá 404. Limite de
`LIMITE_FAVORITAS_POR_USUARIO` por usuário.

`POST /empresas` cadastra uma empresa cliente. Só o admin e o gestor
(`exigir_papel`): o analista recebe 403. O CNPJ tem a mesma validação e
normalização do `/register`, alfanumérico inclusive. Regras, nesta ordem:

- 429 com `Retry-After` depois de `LIMITE_EMPRESAS_POR_HORA` empresas criadas
  pela conta na última hora, contadas na tabela (a primeira, do cadastro,
  inclusive). É uma trava contra abuso, separada do plano, e vale mesmo sem
  limite de plano configurado. A linha da conta é lida com `FOR UPDATE`, o
  mesmo desenho do limite de convites por hora da #69;
- 409 "Empresa já cadastrada nesta conta." quando o CNPJ já é de uma empresa
  da conta. O mesmo CNPJ em outra conta é permitido; a corrida entre dois
  cadastros iguais cai no `UNIQUE (conta_id, cnpj)` e também vira 409;
- 402 no limite de empresas do plano (app/services/plano.py), para todos os
  papéis;
- consulta na Receita da #119, com as regras do cadastro: "nao_encontrado" e
  "inativa" dão 422 com `loc` `["body", "cnpj"]`; "ativa" preenche
  `cnpj_verificado_em`; desligada ou indisponível, a empresa nasce não
  verificada.

A consulta à Receita (até 5 s) roda com a transação encerrada, sem a trava da
conta. Por isso as três primeiras regras são conferidas de novo, com a trava,
depois dela. A empresa nasce com a `Configuracao` padrão. Editar e excluir
empresa ficam fora: a exclusão de uma empresa cliente é a #138.

Logs só com o resultado categorizado; nunca o CNPJ nem a razão social.
"""

import logging
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import DateTime, and_, case, cast, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import (
    CnpjValido,
    ConsultaCnpjDependencia,
    TextoPreenchido,
    barrar_cnpj_recusado,
    cnpj_verificado_em,
    situacao_na_receita,
)
from app.core.auth import exigir_papel, obter_usuario_autenticado
from app.core.database import get_db
from app.core.rate_limit import LIMITE_FAVORITAS_POR_USUARIO, chave_por_usuario, limiter
from app.models import (
    Configuracao,
    Conta,
    Empresa,
    EmpresaFavorita,
    ExecucaoConciliacao,
    Extrato,
    Fechamento,
    Usuario,
)
from app.services.plano import barrar_no_limite, empresas_usadas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresas", tags=["empresas"])

LIMITE_EMPRESAS_POR_HORA = 20

# Acentos do português, maiúsculos e minúsculos, e a letra sem acento. O mesmo
# mapa normaliza a razão social no banco (`translate`) e a busca no Python,
# então os dois lados comparam igual qualquer que seja o locale do Postgres.
_COM_ACENTO = "ÁÀÂÃÄáàâãäÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÕÖóòôõöÚÙÛÜúùûüÇçÑñ"
_SEM_ACENTO = "aaaaaaaaaaeeeeeeeeiiiiiiiioooooooooouuuuuuuuccnn"
_SEM_ACENTO_PYTHON = str.maketrans(_COM_ACENTO, _SEM_ACENTO)
_MASCARA_CNPJ = re.compile(r"[.\-/\s]")

Ordem = Literal["nome", "cnpj", "status"]
Direcao = Literal["asc", "desc"]
StatusEmpresa = Literal["fechado", "em_aberto", "sem_conciliacao"]


class EmpresaRequest(BaseModel):
    cnpj: CnpjValido
    razao_social: TextoPreenchido


class EmpresaResponse(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str
    cnpj_verificado_em: datetime | None
    criado_em: datetime


class EmpresaItemResponse(EmpresaResponse):
    favorita: bool
    status: StatusEmpresa


class EmpresaListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[EmpresaItemResponse]


def _resposta(empresa: Empresa) -> EmpresaResponse:
    # `criado_em` é `TIMESTAMP` sem fuso, gravado em UTC (TimestampMixin).
    return EmpresaResponse(
        id=empresa.id,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        cnpj_verificado_em=empresa.cnpj_verificado_em,
        criado_em=empresa.criado_em.replace(tzinfo=UTC),
    )


def _nome_normalizado(coluna):
    return func.lower(func.translate(coluna, _COM_ACENTO, _SEM_ACENTO))


def _filtro_da_busca(busca: str | None):
    """Condição da busca, ou None sem texto (regra no docstring do módulo)."""
    texto = (busca or "").strip()
    if not texto:
        return None
    condicoes = [
        _nome_normalizado(Empresa.razao_social).contains(
            texto.translate(_SEM_ACENTO_PYTHON).lower(), autoescape=True
        )
    ]
    cnpj = _MASCARA_CNPJ.sub("", texto).upper()
    if cnpj.isalnum() and any(caractere.isdigit() for caractere in cnpj):
        condicoes.append(Empresa.cnpj.contains(cnpj, autoescape=True))
    return or_(*condicoes)


def _status_das_empresas(conta_id: uuid.UUID):
    """`status` de cada empresa da conta como expressão, a partir da
    competência mais recente com conciliação (subconsulta agrupada, uma linha
    por empresa que já conciliou)."""
    competencias = (
        select(
            Extrato.empresa_id,
            func.max(func.to_char(Extrato.periodo_inicio, "YYYY-MM")).label("competencia"),
        )
        .join(ExecucaoConciliacao, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
        .join(Empresa, Empresa.id == Extrato.empresa_id)
        .where(Empresa.conta_id == conta_id, Extrato.periodo_inicio.is_not(None))
        .group_by(Extrato.empresa_id)
        .subquery()
    )
    fechado = exists().where(
        Fechamento.empresa_id == Empresa.id,
        Fechamento.competencia == competencias.c.competencia,
        Fechamento.reaberto_em.is_(None),
    )
    status = case(
        (competencias.c.competencia.is_(None), "sem_conciliacao"),
        (fechado, "fechado"),
        else_="em_aberto",
    )
    return competencias, status


@router.get("", response_model=EmpresaListaResponse)
def listar_empresas(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    busca: Annotated[str | None, Query(max_length=100)] = None,
    ordem: Annotated[Ordem, Query()] = "nome",
    direcao: Annotated[Direcao, Query()] = "asc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EmpresaListaResponse:
    competencias, status_expr = _status_das_empresas(usuario.conta_id)
    favorita = EmpresaFavorita.usuario_id.is_not(None).label("favorita")
    status = status_expr.label("status")
    nome = _nome_normalizado(Empresa.razao_social)
    chave = {"nome": nome, "cnpj": Empresa.cnpj, "status": status}[ordem]

    consulta = (
        select(Empresa, favorita, status, func.count().over().label("total"))
        .outerjoin(competencias, competencias.c.empresa_id == Empresa.id)
        .outerjoin(
            EmpresaFavorita,
            and_(
                EmpresaFavorita.empresa_id == Empresa.id,
                EmpresaFavorita.usuario_id == usuario.id,
            ),
        )
        .order_by(
            favorita.desc(),
            chave.desc() if direcao == "desc" else chave.asc(),
            nome,
            Empresa.id,
        )
        .limit(limit)
        .offset(offset)
    )
    filtros = [Empresa.conta_id == usuario.conta_id]
    filtro = _filtro_da_busca(busca)
    if filtro is not None:
        filtros.append(filtro)
    linhas = db.execute(consulta.where(*filtros)).all()

    if linhas:
        total = linhas[0].total
    elif offset > 0:
        # O offset passou do total: a página vazia não traz o `count(*) OVER ()`.
        total = db.scalar(select(func.count()).select_from(Empresa).where(*filtros))
    else:
        total = 0
    return EmpresaListaResponse(
        total=total,
        limit=limit,
        offset=offset,
        itens=[
            EmpresaItemResponse(
                **_resposta(linha.Empresa).model_dump(),
                favorita=linha.favorita,
                status=linha.status,
            )
            for linha in linhas
        ],
    )


def _empresa_da_conta(db: Session, empresa_id: uuid.UUID, conta_id: uuid.UUID) -> Empresa:
    """A empresa, ou 404 se não existe ou é de outra conta (sem confirmar qual)."""
    empresa = db.scalar(
        select(Empresa).where(Empresa.id == empresa_id, Empresa.conta_id == conta_id)
    )
    if empresa is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    return empresa


@router.put("/{empresa_id}/favorita", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_FAVORITAS_POR_USUARIO, key_func=chave_por_usuario)
def favoritar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    empresa = _empresa_da_conta(db, empresa_id, usuario.conta_id)
    # Favoritar de novo não muda nada, nem o `criado_em`.
    db.execute(
        insert(EmpresaFavorita)
        .values(usuario_id=usuario.id, empresa_id=empresa.id, conta_id=usuario.conta_id)
        .on_conflict_do_nothing(index_elements=["usuario_id", "empresa_id"])
    )
    db.commit()


@router.delete("/{empresa_id}/favorita", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_FAVORITAS_POR_USUARIO, key_func=chave_por_usuario)
def desfavoritar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    empresa = _empresa_da_conta(db, empresa_id, usuario.conta_id)
    db.execute(
        delete(EmpresaFavorita).where(
            EmpresaFavorita.usuario_id == usuario.id, EmpresaFavorita.empresa_id == empresa.id
        )
    )
    db.commit()


def _conferir_com_a_conta_travada(db: Session, conta_id: uuid.UUID, cnpj: str) -> Conta:
    """Trava a linha da conta e aplica, nesta ordem, o 429 por hora, o 409 do
    CNPJ repetido na conta e o 402 do plano. Devolve a conta travada."""
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    # Tudo no relógio do Postgres, que é quem grava `criado_em`. O Retry-After
    # é o tempo até a empresa mais antiga da janela sair dela.
    criadas, segundos_ate_liberar = db.execute(
        select(
            func.count(),
            func.extract(
                "epoch",
                func.min(cast(Empresa.criado_em, DateTime(timezone=True)))
                + timedelta(hours=1)
                - func.now(),
            ),
        ).where(
            Empresa.conta_id == conta_id,
            Empresa.criado_em >= func.now() - timedelta(hours=1),
        )
    ).one()
    if criadas >= LIMITE_EMPRESAS_POR_HORA:
        logger.info("cadastro_empresa resultado=limite_por_hora")
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de empresas cadastradas por hora atingido. Tente novamente mais tarde.",
            headers={"Retry-After": str(max(1, math.ceil(segundos_ate_liberar)))},
        )

    if db.scalar(select(Empresa.id).where(Empresa.conta_id == conta_id, Empresa.cnpj == cnpj)):
        logger.info("cadastro_empresa resultado=cnpj_na_conta")
        raise _ja_cadastrada()

    try:
        barrar_no_limite(conta, "empresas", empresas_usadas(db, conta_id))
    except HTTPException:
        logger.info("cadastro_empresa resultado=limite_do_plano")
        raise
    return conta


def _ja_cadastrada() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="Empresa já cadastrada nesta conta."
    )


@router.post("", response_model=EmpresaResponse, status_code=http_status.HTTP_201_CREATED)
def cadastrar_empresa(
    dados: EmpresaRequest,
    usuario: Annotated[Usuario, Depends(exigir_papel("admin", "gestor"))],
    db: Annotated[Session, Depends(get_db)],
    consulta: ConsultaCnpjDependencia,
) -> EmpresaResponse:
    conta_id = usuario.conta_id
    _conferir_com_a_conta_travada(db, conta_id, dados.cnpj)

    # Encerra a transação (e a trava) antes da chamada externa.
    receita = situacao_na_receita(db, consulta, dados.cnpj)
    if receita.situacao in ("nao_encontrado", "inativa"):
        logger.info("cadastro_empresa resultado=cnpj_%s", receita.situacao)
    barrar_cnpj_recusado(receita, dados.cnpj)

    _conferir_com_a_conta_travada(db, conta_id, dados.cnpj)
    empresa = Empresa(
        id=uuid.uuid4(),
        conta_id=conta_id,
        razao_social=dados.razao_social,
        cnpj=dados.cnpj,
        cnpj_verificado_em=cnpj_verificado_em(receita),
    )
    db.add_all([empresa, Configuracao(empresa_id=empresa.id)])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.info("cadastro_empresa resultado=cnpj_na_conta")
        raise _ja_cadastrada() from exc
    db.refresh(empresa)
    logger.info("cadastro_empresa resultado=cadastrada")
    return _resposta(empresa)
