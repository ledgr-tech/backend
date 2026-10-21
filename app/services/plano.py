"""Bloqueio da conciliação pelo plano (issue #122, regra em app/core/plano.py)
e limites de empresas e usuários por plano (issue #133, ADR-019).

Limites: `empresas_usadas` e `usuarios_usados` são a única conta do que a conta
usa, para o 402 do `POST /empresas` e do `POST /empresa/convites` e para os
`limites` do `GET /me` nunca divergirem. Usuários são os ativos mais os
convites pendentes: o convite pendente já ocupa a vaga, e o aceite não é
barrado pelo limite. `barrar_no_limite` levanta o 402, igual para todos os
papéis, inclusive o admin, que passa do limite trocando de plano.

Conciliação: desde a issue #133, olha a situação da conta da empresa ativa.

Só vale com `PLANO_BLOQUEIO_HABILITADO`, falsa por padrão: a variável é ligada
junto com o gateway de pagamento, porque antes dele quem termina o teste não
tem como pagar.

Ligada, barra só a primeira conciliação de um extrato do banco: aquela em que
ainda não existe nenhuma linha em `execucoes_conciliacao` para ele. Reconciliar
um extrato do banco que já tem execução (um par repetido, ou uma nova versão
do extrato do sistema) continua liberado, pela regra de cobrança da ata de
14/10. Ler, decidir, exportar, fechar o mês e enviar extrato não passam por
aqui.
"""

import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.plano import (
    Recurso,
    Situacao,
    chegou_ao_limite,
    limite_da_conta,
    situacao_da_conta,
)
from app.models import Conta, Convite, Empresa, ExecucaoConciliacao, Usuario
from app.services.convites import condicoes_pendente

MOTIVO_DO_LIMITE: dict[Recurso, str] = {
    "empresas": "Sua conta chegou ao limite de empresas do plano.",
    "usuarios": "Sua conta chegou ao limite de usuários do plano.",
}


def empresas_usadas(db: Session, conta_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).where(Empresa.conta_id == conta_id))


def usuarios_usados(
    db: Session, conta_id: uuid.UUID, *, sem_o_convite_para: str | None = None
) -> int:
    """Usuários ativos mais convites pendentes da conta. `sem_o_convite_para`
    tira da conta o convite pendente desse e-mail, que um convite novo para o
    mesmo e-mail substitui (reenviar não ocupa vaga a mais)."""
    ativos = db.scalar(
        select(func.count()).where(Usuario.conta_id == conta_id, Usuario.removido_em.is_(None))
    )
    convites = select(func.count()).where(Convite.conta_id == conta_id, *condicoes_pendente())
    if sem_o_convite_para is not None:
        convites = convites.where(Convite.email != sem_o_convite_para)
    return ativos + db.scalar(convites)


def barrar_no_limite(conta: Conta, recurso: Recurso, usados: int) -> None:
    """402 quando a conta não pode adicionar mais um do `recurso`."""
    if chegou_ao_limite(usados, limite_da_conta(conta, recurso)):
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED, detail=MOTIVO_DO_LIMITE[recurso]
        )


MOTIVO_DO_BLOQUEIO: dict[Situacao, str] = {
    "teste_encerrado": (
        "Seu período de teste acabou. Escolha um plano para fazer novas conciliações."
    ),
    "sem_plano": "Escolha um plano para começar a conciliar.",
}


def motivo_do_bloqueio(db: Session, empresa: Empresa, extrato_banco_id: uuid.UUID) -> str | None:
    """O `detail` do 402 quando a conciliação deve ser barrada, ou `None`.
    Sem consulta nenhuma com o bloqueio desligado."""
    if not settings.plano_bloqueio_habilitado:
        return None
    motivo = MOTIVO_DO_BLOQUEIO.get(situacao_da_conta(empresa.conta))
    if motivo is None:
        return None
    ja_conciliado = db.scalar(
        select(
            exists().where(
                ExecucaoConciliacao.empresa_id == empresa.id,
                ExecucaoConciliacao.extrato_banco_id == extrato_banco_id,
            )
        )
    )
    return None if ja_conciliado else motivo
