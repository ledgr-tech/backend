"""Remoção de empresa cliente, com 30 dias para restaurar (issue #215, ADR-019).

`DELETE /empresas/{id}` não apaga: marca `empresas.removida_em` e os campos que
vão com ele (`app/models/empresa.py`), e a empresa some de toda consulta pelo
filtro de sessão do model. Passado `apagar_em`, a limpeza diária
(scripts/apagar_empresas_removidas.py) apaga os dados de vez, com a mesma lógica
da exclusão da conta (app/services/exclusao.py).

Nada aqui faz commit: quem chama decide a transação. O relógio é `agora`, o
único do módulo, para os testes de prazo trocarem.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conta, Empresa, Usuario
from app.models.empresa import INCLUIR_REMOVIDAS, PRAZO_PARA_APAGAR
from app.services.conexoes_mcp import revogar_conexoes_da_empresa
from app.services.email import ProvedorDeEmail
from app.services.email.base import MensagemEmail
from app.services.email.envio import enviar_e_registrar
from app.services.email.mensagens import mensagem_empresa_removida

logger = logging.getLogger(__name__)

FUSO = ZoneInfo("America/Sao_Paulo")

# Recusas da remoção: o `codigo` vai ao lado do `detail`, que o front mostra direto.
CODIGO_PRINCIPAL = "empresa_principal"
CODIGO_ULTIMA = "ultima_empresa"
MENSAGEM_PRINCIPAL = (
    "A empresa principal da conta, a do cadastro, não pode ser removida. "
    "Para encerrar a conta, use a desativação da conta."
)
MENSAGEM_ULTIMA = "A última empresa ativa da conta não pode ser removida."


def agora() -> datetime:
    return datetime.now(UTC)


def eh_principal(empresa: Empresa, conta: Conta) -> bool:
    """A empresa principal é a do cadastro: a que tem o mesmo CNPJ da conta."""
    return empresa.cnpj == conta.cnpj


def com_removidas() -> dict[str, bool]:
    return {INCLUIR_REMOVIDAS: True}


@dataclass(frozen=True)
class Remocao:
    """O que a remoção gravou, copiado antes do commit (que expira o objeto)."""

    empresa_id: uuid.UUID
    razao_social: str
    cnpj: str
    removida_em: datetime
    removida_por_nome: str
    apagar_em: datetime


def remover(db: Session, empresa: Empresa, usuario: Usuario) -> Remocao:
    """Marca a empresa como removida por `usuario` e revoga as conexões MCP
    dela, sem commit. As regras (principal, última, papel) são de quem chama."""
    momento = agora()
    empresa.removida_em = momento
    empresa.removida_por_id = usuario.id
    empresa.removida_por_nome = usuario.nome
    empresa.apagar_em = momento + PRAZO_PARA_APAGAR
    revogar_conexoes_da_empresa(db, empresa.id, usuario.id)
    return Remocao(
        empresa_id=empresa.id,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        removida_em=momento,
        removida_por_nome=usuario.nome,
        apagar_em=empresa.apagar_em,
    )


def restaurar(empresa: Empresa) -> None:
    """Volta a empresa ao que era, sem commit. As conexões MCP revogadas na
    remoção não voltam: quem usa conecta de novo."""
    empresa.removida_em = None
    empresa.removida_por_id = None
    empresa.removida_por_nome = None
    empresa.apagar_em = None


def venceu(empresa: Empresa, momento: datetime | None = None) -> bool:
    """O prazo para restaurar acabou: a empresa só espera a limpeza."""
    return empresa.apagar_em is not None and empresa.apagar_em <= (momento or agora())


def _data(momento: datetime) -> str:
    return momento.astimezone(FUSO).strftime("%d/%m/%Y")


@dataclass(frozen=True)
class Destinatario:
    email: str
    nome: str


def destinatarios_do_aviso(db: Session, conta_id: uuid.UUID, quem_removeu: Usuario):
    """O admin da conta e, se for outra pessoa, quem removeu."""
    admin = db.scalar(
        select(Usuario).where(
            Usuario.conta_id == conta_id, Usuario.papel == "admin", Usuario.removido_em.is_(None)
        )
    )
    pessoas = (
        [quem_removeu] if admin is None or admin.id == quem_removeu.id else [admin, quem_removeu]
    )
    return [Destinatario(email=p.email, nome=p.nome) for p in pessoas]


def mensagens_do_aviso(remocao: Remocao, destinatarios: list[Destinatario]) -> list[MensagemEmail]:
    return [
        mensagem_empresa_removida(
            para=d.email,
            nome=d.nome,
            razao_social=remocao.razao_social,
            removida_por=remocao.removida_por_nome,
            restaurar_ate=_data(remocao.apagar_em),
            chave_idempotencia=(
                f"empresa-removida/{remocao.empresa_id}/"
                f"{int(remocao.removida_em.timestamp())}/{i}"
            ),
        )
        for i, d in enumerate(destinatarios)
    ]


def enviar_aviso(provedor: ProvedorDeEmail, mensagens: list[MensagemEmail]) -> None:
    """Roda em `BackgroundTasks`, depois do commit e da resposta. Cada e-mail é
    independente: a falha de um não impede o outro, e nenhuma desfaz a remoção."""
    for mensagem in mensagens:
        enviar_e_registrar(provedor, mensagem, "aviso_empresa_removida")


def agendar_aviso(
    tarefas: BackgroundTasks,
    provedor: ProvedorDeEmail | None,
    remocao: Remocao,
    destinatarios: list[Destinatario],
    enviar: Callable[[ProvedorDeEmail, list[MensagemEmail]], None] = enviar_aviso,
) -> None:
    """Com o e-mail desligado, a remoção vale do mesmo jeito, só sem aviso."""
    if provedor is None:
        logger.info("aviso_empresa_removida resultado=desabilitado")
        return
    tarefas.add_task(enviar, provedor, mensagens_do_aviso(remocao, destinatarios))
