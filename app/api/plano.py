"""Plano da empresa e período de teste (issue #122, regra em app/core/plano.py).

`POST /empresa/plano` escolhe o plano. Só o administrador (403 para os outros,
pela dependency `obter_administrador_autenticado`, como as rotas de equipe da
#69). Plano fora de `PLANOS` é 422 no formato do FastAPI, com `loc`
`["body", "plano"]`.

- A primeira escolha grava o plano e começa o teste: `teste_termina_em` é o
  agora (relógio da #75) mais 14 dias.
- Trocar de plano depois só troca o plano, sem reiniciar o teste.
- O mesmo plano de novo é 200 sem mudar nada.
- Com a assinatura ativa, a troca vale e a situação continua "ativo".

A linha da empresa é lida com `FOR UPDATE`: duas escolhas simultâneas não
começam dois testes. A resposta traz o plano, o fim do teste e a situação,
que é a mesma conta do `GET /me` e do bloqueio do `POST /conciliacoes`. Logs
só com o resultado categorizado.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import plano as regra
from app.core.auth import obter_administrador_autenticado
from app.core.database import get_db
from app.models import Empresa, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresa", tags=["plano"])


class EscolhaPlanoRequest(BaseModel):
    plano: regra.Plano


class PlanoResponse(BaseModel):
    plano: str | None
    teste_termina_em: datetime | None
    situacao: regra.Situacao


def resposta_do_plano(empresa: Empresa, instante: datetime | None = None) -> PlanoResponse:
    """Plano, fim do teste (em UTC) e situação da empresa: o trecho do
    `GET /me` e a resposta do `POST /empresa/plano`."""
    fim = empresa.teste_termina_em
    return PlanoResponse(
        plano=empresa.plano,
        teste_termina_em=fim.astimezone(UTC) if fim is not None else None,
        situacao=regra.situacao_da_empresa(empresa, instante),
    )


@router.post("/plano", response_model=PlanoResponse)
def escolher_plano(
    dados: EscolhaPlanoRequest,
    administrador: Annotated[Usuario, Depends(obter_administrador_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> PlanoResponse:
    empresa = db.scalar(
        select(Empresa)
        .where(Empresa.id == administrador.empresa_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agora = regra.agora()
    if empresa.plano == dados.plano:
        resultado = "mesmo_plano"
    elif empresa.plano is None:
        empresa.plano = dados.plano
        empresa.teste_termina_em = agora + regra.DURACAO_TESTE
        resultado = "teste_iniciado"
    else:
        empresa.plano = dados.plano
        resultado = "plano_trocado"
    resposta = resposta_do_plano(empresa, agora)
    db.commit()
    logger.info("escolha_plano resultado=%s", resultado)
    return resposta
