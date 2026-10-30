"""Plano da conta e período de teste (issue #122, regra em app/core/plano.py).

`POST /empresa/plano` está OBSOLETO (issues #166 e #167, ADR-026) e segue só por
compatibilidade com o front em produção: o teste de 14 dias começa no
cadastro, sem plano, e o plano passa a ser escolhido no checkout
(`POST /assinatura/checkout`). A rota escolhe a preferência de plano da CONTA
(issue #133, ADR-019) e ignora o cabeçalho `X-Ledgr-Empresa`. Só o admin (403
para os outros, pela dependency `obter_admin_autenticado`, como as rotas de
equipe da #69). Plano fora de `PLANOS` é 422 no formato do FastAPI, com `loc`
`["body", "plano"]`.

- Não inicia mais o teste: o cadastro já iniciou. Só grava `contas.plano`.
- Com assinatura viva (`active`, `trialing`, `past_due` ou `incomplete`), 409:
  a troca de plano de quem assina é a rota do PR 2.
- O mesmo plano de novo é 200 sem mudar nada.

A linha da conta é lida com `FOR UPDATE`. A resposta traz o plano, o fim do
teste e a situação, que é a mesma conta do `GET /me` e do bloqueio do
`POST /conciliacoes`. Logs só com o resultado categorizado.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import plano as regra
from app.core.auth import obter_admin_autenticado
from app.core.database import get_db
from app.models import Conta, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresa", tags=["plano"])


JA_ASSINA = "Sua conta já tem uma assinatura. Para trocar de plano, use a área de assinatura."


class EscolhaPlanoRequest(BaseModel):
    plano: regra.Plano


class PlanoResponse(BaseModel):
    plano: str | None
    teste_termina_em: datetime | None
    situacao: regra.Situacao


def resposta_do_plano(conta: Conta, instante: datetime | None = None) -> PlanoResponse:
    """Plano, fim do teste (em UTC) e situação da conta: o trecho do
    `GET /me` e a resposta do `POST /empresa/plano`."""
    fim = conta.teste_termina_em
    return PlanoResponse(
        plano=conta.plano,
        teste_termina_em=fim.astimezone(UTC) if fim is not None else None,
        situacao=regra.situacao_da_conta(conta, instante),
    )


@router.post("/plano", response_model=PlanoResponse, deprecated=True)
def escolher_plano(
    dados: EscolhaPlanoRequest,
    admin: Annotated[Usuario, Depends(obter_admin_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> PlanoResponse:
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == admin.conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assinatura = conta.assinatura_stripe
    if assinatura is not None and assinatura.status in regra.STATUS_EM_ANDAMENTO:
        db.rollback()
        logger.info("escolha_plano resultado=assinatura_viva")
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=JA_ASSINA)
    if conta.plano == dados.plano:
        resultado = "mesmo_plano"
    else:
        conta.plano = dados.plano
        resultado = "plano_gravado"
    resposta = resposta_do_plano(conta, regra.agora())
    db.commit()
    logger.info("escolha_plano resultado=%s", resultado)
    return resposta
