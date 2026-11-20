"""Alerta por e-mail a cada ação de operador no `/admin` (issue #234).

As seis ações que mudam uma conta pelo painel (inativar, reativar, piloto,
cancelar a assinatura, manter a assinatura e dar cupom) avisam a equipe interna
por e-mail: quem agiu, em qual conta, o quê, quando (fuso de São Paulo) e o
motivo. Serve para uma ação indevida de um operador, ou de uma sessão roubada,
não passar despercebida.

- Destinatários: `ALERTAS_ADMIN_EMAILS` (app/core/config.py). Sem ela, com o
  e-mail desligado (`EMAIL_HABILITADO`) ou sem provedor, não envia e só loga
  `admin_alerta resultado=desligado`, sem endereço nenhum.
- `agendar` roda DEPOIS do commit da ação e só enfileira o envio em
  `BackgroundTasks`, que sai depois da resposta: ação recusada (404, 409, 422,
  403, Stripe fora do ar) nunca chega aqui. Nada do que acontece aqui falha a
  ação nem a desfaz: o preparo é protegido por `try`, e o envio usa
  `enviar_e_registrar`, que só registra o resultado.
- Log da regra da issue #34: só `conta_id`, a ação (o tipo do evento) e o
  resultado categorizado, nunca e-mail, nome, razão social nem motivo.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import BackgroundTasks

from app.core.config import settings
from app.models import Conta, Usuario
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar
from app.services.email.mensagens import mensagem_alerta_admin
from app.services.plano import FUSO_DA_FRANQUIA

logger = logging.getLogger(__name__)

SEM_MOTIVO = "sem motivo informado"
# O tipo do evento em `eventos_conta` (app/models/admin.py) para o texto da ação.
ACOES = {
    "inativada": "Conta inativada",
    "reativada": "Conta reativada",
    "piloto_ligado": "Piloto ligado",
    "piloto_desligado": "Piloto desligado",
    "assinatura_cancelada": "Assinatura cancelada na hora",
    "cancelamento_agendado": "Cancelamento da assinatura agendado para o fim do período",
    "cancelamento_desfeito": "Cancelamento da assinatura desfeito (assinatura mantida)",
    "cupom_enviado": "Cupom dado à conta",
}


def agendar(
    tarefas: BackgroundTasks,
    provedor: ProvedorDeEmail | None,
    *,
    operador: Usuario,
    conta: Conta,
    tipo: str,
    motivo: str | None = None,
    detalhe: str | None = None,
) -> None:
    """Enfileira um e-mail por destinatário. `operador` é o usuário que agiu,
    `tipo` o do evento da ação, `motivo` o texto do operador (`None` ou vazio
    vira "sem motivo informado") e `detalhe` completa a ação (o cupom dado,
    por exemplo). Os atributos são lidos aqui dentro, depois do commit: se a
    leitura falhar, é o alerta que se perde, não a ação."""
    conta_id = getattr(conta, "id", None)
    try:
        destinatarios = settings.alertas_admin_destinatarios()
        if provedor is None or not destinatarios:
            logger.info("admin_alerta resultado=desligado")
            return
        acao = ACOES.get(tipo, tipo)
        if detalhe:
            acao = f"{acao}: {detalhe}"
        quando = datetime.now(UTC).astimezone(FUSO_DA_FRANQUIA).strftime("%d/%m/%Y às %H:%M:%S")
        alerta = uuid.uuid4()
        for posicao, para in enumerate(destinatarios):
            mensagem = mensagem_alerta_admin(
                para=para,
                operador_nome=operador.nome,
                operador_email=operador.email,
                conta_id=str(conta_id),
                razao_social=conta.razao_social,
                acao=acao,
                quando=quando,
                motivo=(motivo or "").strip() or SEM_MOTIVO,
                chave_idempotencia=f"admin_alerta/{alerta}/{posicao}",
            )
            tarefas.add_task(
                enviar_e_registrar,
                provedor,
                mensagem,
                f"admin_alerta conta_id={conta_id} acao={tipo}",
            )
    except Exception as erro:  # noqa: BLE001 - a ação já foi gravada, o alerta nunca a derruba
        logger.info(
            "admin_alerta resultado=falha_preparo conta_id=%s acao=%s classe=%s",
            conta_id,
            tipo,
            type(erro).__name__,
        )
