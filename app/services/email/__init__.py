"""Fábrica de provedor de e-mail e reexport dos tipos públicos (issue #66,
ADR-012).

`obter_provedor_email` decide, a partir da configuração, se e qual
provedor instanciar. Hoje só existe "resend". Import de `settings` é
preguiçoso, pelo mesmo motivo de app/services/ia/__init__.py: importar este
pacote não deve exigir `DATABASE_URL` nem `.env`.
"""

from typing import TYPE_CHECKING

from app.services.email.base import MensagemEmail, ProvedorDeEmail, ProvedorEmailIndisponivel
from app.services.email.resend_provider import ProvedorResend

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "MensagemEmail",
    "ProvedorDeEmail",
    "ProvedorEmailIndisponivel",
    "ProvedorResend",
    "obter_provedor_email",
]


def obter_provedor_email(settings: "Settings | None" = None) -> ProvedorDeEmail | None:
    """Nenhum provedor (`None`) quando `email_habilitado` é falso, a chave
    ou o remetente estão vazios, ou `email_provedor` não é reconhecido —
    só "resend" existe hoje."""
    if settings is not None:
        config = settings
    else:
        from app.core.config import settings as config  # import preguiçoso

    if not config.email_habilitado:
        return None
    if config.email_provedor != "resend":
        return None
    if not config.resend_api_key.strip() or not config.email_remetente.strip():
        return None
    return ProvedorResend(
        api_key=config.resend_api_key,
        remetente=config.email_remetente,
        base_url=config.resend_base_url,
        timeout_segundos=config.email_timeout_segundos,
        responder_para=config.email_responder_para,
    )
