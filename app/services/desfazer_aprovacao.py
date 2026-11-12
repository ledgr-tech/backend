"""Quem pode desfazer a aprovação de uma justificativa (issues #164 e #174).

Única regra do `aprovacao_desfeita`, usada pelo `POST /conciliacoes/{id}/decisoes`
(que responde 403/422 conforme o motivo) e pelo campo `pode_desfazer_aprovacao`
da `decisao` do `GET /conciliacoes/{id}` e da resposta do POST (que só diz sim
ou não). Como as duas saem de `motivo_da_recusa`, nunca divergem.

A função não consulta o banco: recebe o contexto já carregado. Num GET de
muitos itens, o que é do usuário e do mês carrega uma vez (`carregar_contexto`)
e os aprovadores da página, numa consulta só; o cálculo por item usa só isso.

Ordem dos motivos, a mesma das validações do POST: o papel (o analista nunca
aprova), a flag `APROVACAO_JUSTIFICATIVAS_HABILITADA`, o mês, a existência de
uma justificativa aprovada em vigor e, por último, quem aprovou. Só quem
aprovou desfaz; o admin desfaz também quando quem aprovou foi removido, saiu da
conta ou não é mais admin nem gestor, e na aprovação da migração da #132, que
não é de ninguém (senão elas ficariam impossíveis de desfazer).
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Extrato, Usuario
from app.services import decisoes
from app.services.fechamentos import fechamento_ativo
from app.services.travas import competencia_do_extrato

# Quem aprova e rejeita justificativas (issue #132, ADR-019).
PAPEIS_QUE_APROVAM = ("admin", "gestor")


class Recusa(StrEnum):
    PERMITIDO = "permitido"
    SEM_PAPEL = "sem_papel"
    FLAG_DESLIGADA = "flag_desligada"
    MES_FECHADO = "mes_fechado"
    SEM_APROVACAO = "sem_aprovacao"
    SO_QUEM_APROVOU = "so_quem_aprovou"


@dataclass(frozen=True)
class Aprovador:
    """O que a regra precisa saber de quem aprovou."""

    removido: bool
    conta_id: uuid.UUID
    papel: str


@dataclass(frozen=True)
class ContextoDesfazer:
    usuario_id: uuid.UUID
    conta_id: uuid.UUID
    papel: str
    flag_ligada: bool
    mes_fechado: bool
    # Por `usuario_id` do evento de aprovação; quem não está aqui não existe mais.
    aprovadores: Mapping[uuid.UUID, Aprovador] = field(default_factory=dict)


def _aprovacao_da_migracao(aprovacao) -> bool:
    return aprovacao.autor_nome == decisoes.AUTOR_APROVACAO_DA_MIGRACAO


def motivo_da_recusa(contexto: ContextoDesfazer, vigente: decisoes.DecisaoEmVigor | None) -> Recusa:
    """`Recusa.PERMITIDO` quando o usuário do contexto pode desfazer a
    aprovação da decisão em vigor agora; senão, o primeiro motivo que impede."""
    if contexto.papel not in PAPEIS_QUE_APROVAM:
        return Recusa.SEM_PAPEL
    if not contexto.flag_ligada:
        return Recusa.FLAG_DESLIGADA
    if contexto.mes_fechado:
        return Recusa.MES_FECHADO
    if not decisoes.justificativa_aprovada(vigente):
        return Recusa.SEM_APROVACAO

    aprovacao = vigente.aprovacao
    da_migracao = _aprovacao_da_migracao(aprovacao)
    if not da_migracao and aprovacao.usuario_id == contexto.usuario_id:
        return Recusa.PERMITIDO
    if contexto.papel != "admin":
        return Recusa.SO_QUEM_APROVOU
    if da_migracao:
        return Recusa.PERMITIDO
    aprovador = contexto.aprovadores.get(aprovacao.usuario_id)
    if (
        aprovador is None
        or aprovador.removido
        or aprovador.conta_id != contexto.conta_id
        or aprovador.papel not in PAPEIS_QUE_APROVAM
    ):
        return Recusa.PERMITIDO
    return Recusa.SO_QUEM_APROVOU


def pode_desfazer_aprovacao(
    contexto: ContextoDesfazer, vigente: decisoes.DecisaoEmVigor | None
) -> bool:
    return motivo_da_recusa(contexto, vigente) is Recusa.PERMITIDO


def contexto_basico(usuario: Usuario) -> ContextoDesfazer:
    """O que só depende de quem faz a requisição e da flag, sem consulta: basta
    para o motivo `SEM_PAPEL` e para o `FLAG_DESLIGADA`."""
    return ContextoDesfazer(
        usuario_id=usuario.id,
        conta_id=usuario.conta_id,
        papel=usuario.papel,
        flag_ligada=settings.aprovacao_justificativas_habilitada,
        mes_fechado=False,
    )


def carregar_contexto(
    db: Session,
    usuario: Usuario,
    empresa_id: uuid.UUID,
    extrato_banco: Extrato,
    vigentes: Iterable[decisoes.DecisaoEmVigor | None],
) -> ContextoDesfazer:
    """O contexto de quem faz a requisição para as decisões em vigor da
    página, com o mínimo de consultas e nenhuma por item: o fechamento do mês
    (uma) e os aprovadores de que o admin precisa (uma), e só quando alguma
    linha tem justificativa aprovada e o papel e a flag deixam desfazer."""
    contexto = contexto_basico(usuario)
    aprovacoes = [v.aprovacao for v in vigentes if decisoes.justificativa_aprovada(v)]
    if not aprovacoes or not contexto.flag_ligada or usuario.papel not in PAPEIS_QUE_APROVAM:
        return contexto

    competencia = competencia_do_extrato(extrato_banco)
    mes_fechado = (
        competencia is not None and fechamento_ativo(db, empresa_id, competencia) is not None
    )
    aprovadores: dict[uuid.UUID, Aprovador] = {}
    if usuario.papel == "admin":
        # O gestor só desfaz a própria: não precisa dos dados de ninguém.
        ids = {
            a.usuario_id
            for a in aprovacoes
            if a.usuario_id != usuario.id and not _aprovacao_da_migracao(a)
        }
        if ids:
            aprovadores = {
                linha.id: Aprovador(
                    removido=linha.removido_em is not None,
                    conta_id=linha.conta_id,
                    papel=linha.papel,
                )
                for linha in db.execute(
                    select(Usuario.id, Usuario.removido_em, Usuario.conta_id, Usuario.papel).where(
                        Usuario.id.in_(sorted(ids))
                    )
                )
            }
    return replace(contexto, mes_fechado=mes_fechado, aprovadores=aprovadores)
