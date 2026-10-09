"""Envio de e-mail transacional a partir das rotas (issues #66 e #78).

Saiu de app/api/senha.py para o cadastro (app/api/auth.py) também enviar:
senha.py já importa de auth.py, e o caminho inverso seria import circular.

`obter_provedor_email_dependencia` é a dependency que as rotas usam (e que os
testes trocam pelo provedor falso). `enviar_e_registrar` roda em
`BackgroundTasks`, depois da resposta: uma falha não tem a quem ser devolvida,
então só fica no log.

Nunca loga destinatário, nome, assunto nem conteúdo da mensagem: só o evento,
o resultado e, na falha, o motivo ou a classe da exceção (regra da issue #34).
"""

import logging

from app.services.email import ProvedorDeEmail, ProvedorEmailIndisponivel, obter_provedor_email
from app.services.email.base import MensagemEmail

logger = logging.getLogger(__name__)


def obter_provedor_email_dependencia() -> ProvedorDeEmail | None:
    return obter_provedor_email()


def enviar_e_registrar(provedor: ProvedorDeEmail, mensagem: MensagemEmail, evento: str) -> None:
    """Envia e só registra o resultado: `<evento> resultado=enviado|falha_envio`."""
    try:
        provedor.enviar(mensagem)
    except ProvedorEmailIndisponivel as exc:
        logger.info(
            "%s resultado=falha_envio motivo=%s status_http=%s",
            evento,
            exc.motivo,
            exc.status_http,
        )
    except Exception as exc:  # noqa: BLE001 - a resposta já saiu, só registra
        logger.info("%s resultado=falha_envio classe=%s", evento, type(exc).__name__)
    else:
        logger.info("%s resultado=enviado", evento)
