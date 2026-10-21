"""Aprovação e rejeição de justificativas em lote (issue #132, ADR-019).

`POST /justificativas/lote` aprova ou rejeita, de uma vez, as justificativas
aguardando aprovação, pela empresa ativa (`X-Ledgr-Empresa`). Só o admin e o
gestor: o analista recebe 403 (`exigir_papel`). É o que a tela de fechamento
usa para aprovar as pendentes da competência antes de fechar. A aprovação e a
rejeição uma a uma estão em `POST /conciliacoes/{extrato_id}/decisoes`, com as
mesmas regras (app/services/decisoes.py).

Corpo: `acao` ("aprovar" ou "rejeitar"), `motivo` (opcional, só na rejeição,
com o tamanho máximo da justificativa) e exatamente um entre `competencia`
("AAAA-MM") e `itens` (até `MAXIMO_ITENS` pares `extrato_banco_id` e `chave`).
Os dois, ou nenhum, é 422.

- Com `competencia`: todas as justificativas aguardando aprovação dos pares da
  competência, com a regra de pares do fechamento (a rodada mais recente de
  cada extrato do banco, app/services/fechamentos.py).
- Com `itens`: só os itens que estão aguardando aprovação na rodada mais
  recente do extrato do banco; os outros (já decididos, linha que não diverge,
  extrato de outra empresa ou inexistente, repetidos) são ignorados e
  contados em `ignoradas`, sem erro.

Tudo numa transação, com as travas na ordem fixa de app/services/travas.py:
as do mês (compartilhadas, em ordem de competência) e depois as dos extratos
do banco envolvidos, em ordem de id. Mês fechado é 409, com a mesma mensagem
das outras decisões, e nada é gravado. Cada aprovação ou rejeição é um evento
em `decisoes_linha`, de quem chamou, na rodada mais recente.

Resposta: `{ processadas, ignoradas }`. Nunca loga o motivo nem a chave.
"""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.conciliacoes import TAMANHO_MAXIMO_JUSTIFICATIVA
from app.core.auth import EmpresaAtiva, exigir_papel, obter_empresa_ativa
from app.core.database import get_db
from app.models import DecisaoLinha, Extrato, Usuario
from app.services import decisoes, fechamentos
from app.services.travas import (
    competencia_do_extrato,
    travar_extrato_banco,
    travar_mes_compartilhada,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/justificativas", tags=["justificativas"])

MAXIMO_ITENS = 500

obter_admin_ou_gestor = exigir_papel("admin", "gestor")


class ItemLote(BaseModel):
    extrato_banco_id: uuid.UUID
    chave: Annotated[str, Field(min_length=1, max_length=64)]


class LoteRequest(BaseModel):
    acao: Literal["aprovar", "rejeitar"]
    motivo: Annotated[str | None, Field(max_length=TAMANHO_MAXIMO_JUSTIFICATIVA)] = None
    competencia: str | None = None
    itens: Annotated[list[ItemLote] | None, Field(min_length=1, max_length=MAXIMO_ITENS)] = None

    @model_validator(mode="after")
    def _um_entre_competencia_e_itens(self) -> "LoteRequest":
        if (self.competencia is None) == (self.itens is None):
            raise ValueError("informe exatamente um entre competencia e itens")
        if self.competencia is not None and not fechamentos.competencia_valida(self.competencia):
            raise ValueError("competencia deve estar no formato AAAA-MM, com mês de 01 a 12")
        motivo = (self.motivo or "").strip() or None
        if self.acao == "aprovar" and motivo is not None:
            raise ValueError("o motivo só vale na rejeição")
        self.motivo = motivo
        return self


class LoteResponse(BaseModel):
    processadas: int
    ignoradas: int


def _mes_fechado(competencia: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=f"A competência {competencia} está fechada. Reabra o fechamento para alterar.",
    )


def _travar(db: Session, empresa_id: uuid.UUID, extratos: list[Extrato]) -> None:
    """Travas na ordem fixa: os meses (compartilhada, em ordem de competência)
    e depois os extratos do banco, em ordem de id. 409 se algum mês estiver
    fechado."""
    competencias = sorted(
        {c for c in (competencia_do_extrato(extrato) for extrato in extratos) if c is not None}
    )
    for competencia in competencias:
        travar_mes_compartilhada(db, empresa_id, competencia)
    for competencia in competencias:
        if fechamentos.fechamento_ativo(db, empresa_id, competencia) is not None:
            raise _mes_fechado(competencia)
    for extrato in sorted(extratos, key=lambda e: e.id):
        travar_extrato_banco(db, empresa_id, extrato.id)


@router.post("/lote", response_model=LoteResponse)
def decidir_em_lote(
    corpo: LoteRequest,
    _papel: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> LoteResponse:
    usuario, empresa_id = ativa.usuario, ativa.empresa.id

    if corpo.competencia is not None:
        # A trava do mês vem antes de olhar os extratos dele, e a conferência
        # do fechamento vale mesmo sem extrato nenhum.
        travar_mes_compartilhada(db, empresa_id, corpo.competencia)
        if fechamentos.fechamento_ativo(db, empresa_id, corpo.competencia) is not None:
            raise _mes_fechado(corpo.competencia)
        extratos = list(
            db.scalars(
                select(Extrato).where(
                    Extrato.empresa_id == empresa_id,
                    Extrato.origem == "banco",
                    func.to_char(Extrato.periodo_inicio, "YYYY-MM") == corpo.competencia,
                )
            )
        )
    else:
        extratos = list(
            db.scalars(
                select(Extrato).where(
                    Extrato.empresa_id == empresa_id,
                    Extrato.origem == "banco",
                    Extrato.id.in_({item.extrato_banco_id for item in corpo.itens}),
                )
            )
        )
    _travar(db, empresa_id, extratos)

    # A rodada é calculada depois das travas dos extratos, como no POST de
    # decisões: uma rodada nova no meio espera, ou é esperada.
    rodadas = decisoes.rodadas_mais_recentes(db, empresa_id, [e.id for e in extratos])
    aguardando = decisoes.justificativas_aguardando(
        db,
        empresa_id,
        {(banco, rodada.extrato_sistema_id) for banco, rodada in rodadas.items()},
    )

    if corpo.itens is None:
        alvos = sorted(aguardando)
        ignoradas = 0
    else:
        pedidos = [(item.extrato_banco_id, item.chave) for item in corpo.itens]
        alvos = sorted(set(pedidos) & set(aguardando))
        ignoradas = len(pedidos) - len(alvos)

    tipo = "justificativa_aprovada" if corpo.acao == "aprovar" else "justificativa_rejeitada"
    for banco, chave in alvos:
        rodada = rodadas[banco]
        db.add(
            DecisaoLinha(
                empresa_id=empresa_id,
                extrato_banco_id=banco,
                chave=chave,
                tipo=tipo,
                texto=corpo.motivo,
                usuario_id=usuario.id,
                autor_nome=usuario.nome,
                rodada=rodada.numero,
                extrato_sistema_id=rodada.extrato_sistema_id,
            )
        )
    db.commit()
    logger.info(
        "justificativas_lote acao=%s processadas=%d ignoradas=%d",
        corpo.acao,
        len(alvos),
        ignoradas,
    )
    return LoteResponse(processadas=len(alvos), ignoradas=ignoradas)
