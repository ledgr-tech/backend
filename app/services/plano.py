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
junto com as chaves live da Stripe (ADR-026), porque antes disso quem termina o
teste não tem como pagar.

Ligada, barra só a primeira conciliação de um extrato do banco: aquela em que
ainda não existe nenhuma linha em `execucoes_conciliacao` para ele. Reconciliar
um extrato do banco que já tem execução (um par repetido, ou uma nova versão
do extrato do sistema) continua liberado, pela regra de cobrança da ata de
14/10. Ler, decidir, exportar, fechar o mês e enviar extrato não passam por
aqui. Dois motivos de barrar:

- a situação da conta (`teste_encerrado`, `sem_plano`, `assinatura_encerrada`);
- a franquia do plano (issues #166 e #167, ADR-026): a conta já fez, no mês
  civil de São Paulo, tantas primeiras conciliações quantas o plano efetivo dá
  (`LIMITE_CONCILIACOES_<PLANO>`). `conciliacoes_no_mes` é a única conta do
  que a conta usou, para o 402 e os `limites` do `GET /me` nunca divergirem.
  Conta todas as empresas da conta, mesmo nos planos trimestral, semestral e
  anual, e o mês vira à meia-noite de São Paulo.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.plano import (
    Recurso,
    Situacao,
    agora,
    chegou_ao_limite,
    limite_da_conta,
    situacao_da_conta,
)
from app.models import Conta, Convite, Empresa, ExecucaoConciliacao, Usuario
from app.services.convites import condicoes_pendente

FUSO_DA_FRANQUIA = ZoneInfo("America/Sao_Paulo")

MOTIVO_DO_LIMITE: dict[Recurso, str] = {
    "empresas": "Sua conta chegou ao limite de empresas do plano.",
    "usuarios": "Sua conta chegou ao limite de usuários do plano.",
    "conciliacoes": (
        "Sua conta chegou ao limite de conciliações do mês no plano. "
        "Troque de plano para conciliar mais extratos."
    ),
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


def mes_civil_de_sao_paulo(instante: datetime) -> tuple[datetime, datetime]:
    """O início (inclusive) e o fim (exclusive) do mês civil de São Paulo que
    contém `instante`, em UTC. São Paulo não tem horário de verão desde 2019,
    mas a conta passa pelo fuso, não por um deslocamento fixo."""
    local = instante.astimezone(FUSO_DA_FRANQUIA)
    inicio = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Dia 1 do mês seguinte: 32 dias depois do dia 1 sempre cai no mês seguinte.
    seguinte = (inicio + timedelta(days=32)).replace(day=1)
    return inicio.astimezone(UTC), seguinte.astimezone(UTC)


def conciliacoes_no_mes(db: Session, conta_id: uuid.UUID, instante: datetime | None = None) -> int:
    """Quantos extratos do banco da conta (todas as empresas) tiveram a PRIMEIRA
    execução no mês civil de São Paulo de `instante`: a unidade da franquia, a
    mesma do bloqueio e do uso do `/admin`."""
    inicio, fim = mes_civil_de_sao_paulo(instante or agora())
    primeiras = (
        select(func.min(ExecucaoConciliacao.criado_em).label("primeira"))
        .where(
            ExecucaoConciliacao.empresa_id.in_(
                select(Empresa.id).where(Empresa.conta_id == conta_id)
            )
        )
        .group_by(ExecucaoConciliacao.extrato_banco_id)
        .subquery()
    )
    return db.scalar(
        select(func.count())
        .select_from(primeiras)
        .where(primeiras.c.primeira >= inicio, primeiras.c.primeira < fim)
    )


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
    "assinatura_encerrada": (
        "Sua assinatura foi encerrada. Assine de novo para fazer novas conciliações."
    ),
}


def motivo_do_bloqueio(db: Session, empresa: Empresa, extrato_banco_id: uuid.UUID) -> str | None:
    """O `detail` do 402 quando a conciliação deve ser barrada, ou `None`.
    Sem consulta nenhuma com o bloqueio desligado."""
    if not settings.plano_bloqueio_habilitado:
        return None
    conta = empresa.conta
    motivo = MOTIVO_DO_BLOQUEIO.get(situacao_da_conta(conta))
    if motivo is None and limite_da_conta(conta, "conciliacoes") is None:
        return None
    ja_conciliado = db.scalar(
        select(
            exists().where(
                ExecucaoConciliacao.empresa_id == empresa.id,
                ExecucaoConciliacao.extrato_banco_id == extrato_banco_id,
            )
        )
    )
    if ja_conciliado:
        return None
    if motivo is not None:
        return motivo
    usadas = conciliacoes_no_mes(db, conta.id)
    if chegou_ao_limite(usadas, limite_da_conta(conta, "conciliacoes")):
        return MOTIVO_DO_LIMITE["conciliacoes"]
    return None
