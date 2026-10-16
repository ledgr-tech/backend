"""Bloqueio da conciliação pelo plano (issue #122, regra em app/core/plano.py).

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

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.plano import Situacao, situacao_da_empresa
from app.models import Empresa, ExecucaoConciliacao

MOTIVO_DO_BLOQUEIO: dict[Situacao, str] = {
    "teste_encerrado": (
        "Seu período de teste acabou. Escolha um plano para fazer novas conciliações."
    ),
    "sem_plano": "Escolha um plano para começar a conciliar.",
}


def motivo_do_bloqueio(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID
) -> str | None:
    """O `detail` do 402 quando a conciliação deve ser barrada, ou `None`.
    Sem consulta nenhuma com o bloqueio desligado."""
    if not settings.plano_bloqueio_habilitado:
        return None
    motivo = MOTIVO_DO_BLOQUEIO.get(situacao_da_empresa(db.get(Empresa, empresa_id)))
    if motivo is None:
        return None
    ja_conciliado = db.scalar(
        select(
            exists().where(
                ExecucaoConciliacao.empresa_id == empresa_id,
                ExecucaoConciliacao.extrato_banco_id == extrato_banco_id,
            )
        )
    )
    return None if ja_conciliado else motivo
