"""Tipos e interface da camada de e-mail (issue #66, ADR-012).

`ProvedorDeEmail` é a interface que qualquer provedor de e-mail
transacional precisa cumprir — o Resend hoje
(app/services/email/resend_provider.py). Síncrona, pelo mesmo motivo da
camada de IA (ADR-011): o envio roda em `BackgroundTasks`, que executa
função síncrona em threadpool.

Nunca logar destinatário, corpo, link nem a chave. A mensagem de
`ProvedorEmailIndisponivel` também nunca contém corpo de resposta — só o
motivo categorizado.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

MotivoIndisponibilidade = Literal["timeout", "http", "nao_configurado"]


@dataclass(frozen=True)
class MensagemEmail:
    """Um e-mail pronto pra envio. `chave_idempotencia` evita envio
    duplicado se a mesma mensagem for enviada de novo (o Resend guarda a
    chave por 24 horas)."""

    para: str
    assunto: str
    texto: str
    html: str
    chave_idempotencia: str | None = None


class ProvedorDeEmail(Protocol):
    """Interface que qualquer provedor de e-mail precisa cumprir (ADR-012)."""

    def enviar(self, mensagem: MensagemEmail) -> None: ...


class ProvedorEmailIndisponivel(Exception):
    """Levantada quando o provedor não aceita o envio. `motivo` é sempre uma
    das categorias fixas: "timeout", "http" ou "nao_configurado".
    `status_http` só é preenchido pra motivo="http" quando existe um código
    de resposta."""

    def __init__(self, motivo: MotivoIndisponibilidade, status_http: int | None = None) -> None:
        self.motivo = motivo
        self.status_http = status_http
        super().__init__(f"Provedor de e-mail indisponível: {motivo}")
