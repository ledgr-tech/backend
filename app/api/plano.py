"""Plano da conta e período de teste (issue #122, regra em app/core/plano.py).

`POST /empresa/plano` escolhe o plano da CONTA (issue #133, ADR-019): o
caminho ficou o mesmo, mas o plano não é de uma empresa cliente, e a rota
ignora o cabeçalho `X-Ledgr-Empresa`. Só o admin (403 para os outros, pela
dependency `obter_admin_autenticado`, como as rotas de equipe da #69). Plano fora de `PLANOS` é 422 no formato do FastAPI, com `loc`
`["body", "plano"]`.

- A primeira escolha grava o plano e começa o teste: `teste_termina_em` é o
  agora (relógio da #75) mais 14 dias.
- Trocar de plano depois só troca o plano, sem reiniciar o teste.
- O mesmo plano de novo é 200 sem mudar nada.
- Com a assinatura ativa, a troca vale e a situação continua "ativo".

A linha da conta é lida com `FOR UPDATE`: duas escolhas simultâneas não
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
from app.core.auth import obter_admin_autenticado
from app.core.database import get_db
from app.models import Conta, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresa", tags=["plano"])


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


@router.post("/plano", response_model=PlanoResponse)
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
    agora = regra.agora()
    if conta.plano == dados.plano:
        resultado = "mesmo_plano"
    elif conta.plano is None:
        conta.plano = dados.plano
        conta.teste_termina_em = agora + regra.DURACAO_TESTE
        resultado = "teste_iniciado"
    else:
        conta.plano = dados.plano
        resultado = "plano_trocado"
    resposta = resposta_do_plano(conta, agora)
    db.commit()
    logger.info("escolha_plano resultado=%s", resultado)
    return resposta
