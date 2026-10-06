"""Fechamento do mês (issue #86, ADR-014): `POST /fechamentos` fecha uma
competência, `GET /fechamentos` lista o histórico e
`DELETE /fechamentos/{competencia}` reabre.

Regra do mês e resumo em app/services/fechamentos.py. Sempre pela empresa do
token; conta única, sem papéis nem 403. Fechar e reabrir pegam a trava
EXCLUSIVA do mês (app/services/travas.py), então esperam qualquer conciliação
ou decisão em andamento naquele mês e as seguintes esperam por eles.

Erros de regra saem com `detail` em texto, porque o front mostra o motivo
direto. Com o mês fechado, `POST /conciliacoes` e `POST
/conciliacoes/{id}/decisoes` de extratos do banco daquela competência dão 409
(app/api/conciliacoes.py). O upload de extrato não é bloqueado: a competência
só existe depois do processamento (limitação conhecida).

Resumo congelado gravado em cada fechamento:

    {
      "pares": [{"extrato_banco_id", "extrato_sistema_id", "execucao_id",
                 "rodada", "nome_arquivo_banco", "nome_arquivo_sistema"}],
      "contagens": {"total", "match_exato", "match_tolerancia", "duplicado",
                    "sem_correspondencia", "tarifa_bancaria",
                    "divergente_valor", "divergente_data"},
      "justificadas": int, "pendentes": int, "linhas_nao_lidas": int,
      "valor_em_aberto": "0.00"
    }

Nunca loga a ressalva nem descrição de lançamento.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import obter_empresa_id_autenticada, obter_usuario_autenticado
from app.core.database import get_db
from app.models import Fechamento, Usuario
from app.services import fechamentos
from app.services.travas import travar_mes

router = APIRouter(prefix="/fechamentos", tags=["fechamentos"])

TAMANHO_MAXIMO_RESSALVA = 1000


class ParResumoResponse(BaseModel):
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    execucao_id: uuid.UUID
    rodada: int
    nome_arquivo_banco: str
    nome_arquivo_sistema: str


class ContagensResumoResponse(BaseModel):
    total: int
    match_exato: int
    match_tolerancia: int
    duplicado: int
    sem_correspondencia: int
    tarifa_bancaria: int
    divergente_valor: int
    divergente_data: int


class ResumoResponse(BaseModel):
    pares: list[ParResumoResponse]
    contagens: ContagensResumoResponse
    justificadas: int
    pendentes: int
    linhas_nao_lidas: int
    valor_em_aberto: str


class FechamentoResponse(BaseModel):
    id: uuid.UUID
    competencia: str
    estado: Literal["fechado", "reaberto"]
    ressalva: str | None
    fechado_por: str
    fechado_em: datetime
    reaberto_por: str | None
    reaberto_em: datetime | None
    resumo: ResumoResponse


class FechamentoListaResponse(BaseModel):
    itens: list[FechamentoResponse]


class FechamentoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competencia: str
    ressalva: str | None = None


def _resposta(fechamento: Fechamento) -> FechamentoResponse:
    return FechamentoResponse(
        id=fechamento.id,
        competencia=fechamento.competencia,
        estado="fechado" if fechamento.reaberto_em is None else "reaberto",
        ressalva=fechamento.ressalva,
        fechado_por=fechamento.fechado_por_nome,
        fechado_em=fechamento.fechado_em,
        reaberto_por=fechamento.reaberto_por_nome,
        reaberto_em=fechamento.reaberto_em,
        resumo=ResumoResponse.model_validate(fechamento.resumo),
    )


def _erro(status_code: int, detalhe: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detalhe)


def _validar_competencia(competencia: str) -> None:
    if not fechamentos.competencia_valida(competencia):
        raise _erro(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A competência deve estar no formato AAAA-MM, com mês de 01 a 12.",
        )


def _plural(quantidade: int, singular: str, plural: str) -> str:
    return f"{quantidade} {singular if quantidade == 1 else plural}"


def _o_que_falta(pendentes: int, linhas_nao_lidas: int) -> str:
    partes = []
    if pendentes:
        partes.append(
            _plural(pendentes, "divergência sem justificativa", "divergências sem justificativa")
        )
    if linhas_nao_lidas:
        partes.append(_plural(linhas_nao_lidas, "linha não lida", "linhas não lidas"))
    return f"Há {' e '.join(partes)} nesta competência. Justifique, corrija ou feche com ressalva."


@router.post(
    "",
    response_model=FechamentoResponse,
    status_code=http_status.HTTP_201_CREATED,
)
def fechar_competencia(
    corpo: FechamentoRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> FechamentoResponse:
    empresa_id = usuario.empresa_id
    _validar_competencia(corpo.competencia)
    ressalva = (corpo.ressalva or "").strip() or None
    if ressalva is not None and len(ressalva) > TAMANHO_MAXIMO_RESSALVA:
        raise _erro(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A ressalva pode ter até {TAMANHO_MAXIMO_RESSALVA} caracteres.",
        )

    travar_mes(db, empresa_id, corpo.competencia)

    if fechamentos.fechamento_ativo(db, empresa_id, corpo.competencia) is not None:
        raise _erro(http_status.HTTP_409_CONFLICT, "Esta competência já está fechada.")

    pares = fechamentos.pares_do_mes(db, empresa_id, corpo.competencia)
    if not pares:
        raise _erro(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, "Não há conciliação nesta competência."
        )

    resumo = fechamentos.resumo_do_mes(db, empresa_id, pares)
    if (resumo["pendentes"] or resumo["linhas_nao_lidas"]) and ressalva is None:
        raise _erro(
            http_status.HTTP_409_CONFLICT,
            _o_que_falta(resumo["pendentes"], resumo["linhas_nao_lidas"]),
        )

    fechamento = Fechamento(
        empresa_id=empresa_id,
        competencia=corpo.competencia,
        ressalva=ressalva,
        resumo=resumo,
        fechado_por_id=usuario.id,
        fechado_por_nome=usuario.nome,
    )
    db.add(fechamento)
    db.commit()
    db.refresh(fechamento)
    return _resposta(fechamento)


@router.get("", response_model=FechamentoListaResponse)
def listar_fechamentos(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    competencia: Annotated[str | None, Query()] = None,
) -> FechamentoListaResponse:
    consulta = select(Fechamento).where(Fechamento.empresa_id == empresa_id)
    if competencia is not None:
        _validar_competencia(competencia)
        consulta = consulta.where(Fechamento.competencia == competencia)
    itens = db.scalars(
        consulta.order_by(Fechamento.competencia.desc(), Fechamento.fechado_em.desc())
    ).all()
    return FechamentoListaResponse(itens=[_resposta(fechamento) for fechamento in itens])


@router.delete("/{competencia}", response_model=FechamentoResponse)
def reabrir_competencia(
    competencia: str,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> FechamentoResponse:
    empresa_id = usuario.empresa_id
    _validar_competencia(competencia)

    travar_mes(db, empresa_id, competencia)

    fechamento = fechamentos.fechamento_ativo(db, empresa_id, competencia)
    if fechamento is None:
        raise _erro(http_status.HTTP_404_NOT_FOUND, "Não há fechamento ativo nesta competência.")

    fechamento.reaberto_por_id = usuario.id
    fechamento.reaberto_por_nome = usuario.nome
    fechamento.reaberto_em = func.clock_timestamp()
    db.commit()
    db.refresh(fechamento)
    return _resposta(fechamento)
