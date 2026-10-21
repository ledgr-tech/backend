"""Empresas clientes da conta (issue #133, ADR-019).

Rotas da conta: ignoram o cabeçalho `X-Ledgr-Empresa`. São elas que dizem
quais empresas o front pode mandar nele.

`GET /empresas` lista todas as empresas da conta, para todos os papéis,
ordenadas pela razão social e depois pelo id.

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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import DateTime, cast, func, select
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
from app.models import Configuracao, Conta, Empresa, Usuario
from app.services.plano import barrar_no_limite, empresas_usadas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresas", tags=["empresas"])

LIMITE_EMPRESAS_POR_HORA = 20


class EmpresaRequest(BaseModel):
    cnpj: CnpjValido
    razao_social: TextoPreenchido


class EmpresaResponse(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str
    cnpj_verificado_em: datetime | None
    criado_em: datetime


class EmpresaListaResponse(BaseModel):
    itens: list[EmpresaResponse]


def _resposta(empresa: Empresa) -> EmpresaResponse:
    # `criado_em` é `TIMESTAMP` sem fuso, gravado em UTC (TimestampMixin).
    return EmpresaResponse(
        id=empresa.id,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        cnpj_verificado_em=empresa.cnpj_verificado_em,
        criado_em=empresa.criado_em.replace(tzinfo=UTC),
    )


@router.get("", response_model=EmpresaListaResponse)
def listar_empresas(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> EmpresaListaResponse:
    empresas = db.scalars(
        select(Empresa)
        .where(Empresa.conta_id == usuario.conta_id)
        .order_by(Empresa.razao_social, Empresa.id)
    )
    return EmpresaListaResponse(itens=[_resposta(empresa) for empresa in empresas])


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
