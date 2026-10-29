"""Link público do relatório do fechamento (issue #165, ADR-024).

Regras do token e do relatório em app/services/relatorio_publico.py.

Rotas da empresa ativa (`X-Ledgr-Empresa`), só do admin e do gestor
(`exigir_papel`, 403 para o analista antes de tudo):

- `POST /fechamentos/{competencia}/links` (`validade_dias` de 1 a 90, padrão
  30): cria o link do fechamento ativo da competência e devolve o token em
  `token`, só nesta resposta. O front monta o endereço com o token no
  fragmento (`/relatorio#<token>`). Sem fechamento ativo: 404 "Não há
  fechamento ativo nesta competência.". Com `LIMITE_ATIVOS` links ativos no
  fechamento: 409. Pega a trava COMPARTILHADA do mês e trava a linha do
  fechamento (`FOR UPDATE`): espera o reabrir, que pega a exclusiva, e duas
  criações simultâneas não passam as duas do limite.
- `GET /fechamentos/{competencia}/links`: os links de todos os fechamentos da
  competência, dos mais novos para os mais antigos, nunca o token nem o
  hash.
- `DELETE /fechamentos/{competencia}/links/{link_id}`: revoga na hora (204).
  Já revogado: 204 sem mudar nada. De outra empresa, de outra competência ou
  inexistente: 404 "Link não encontrado.".

Reabrir o mês (`DELETE /fechamentos/{competencia}`, app/api/fechamentos.py)
revoga os links do fechamento com o motivo "reabertura".

Rota pública, sem login: `POST /relatorios-publicos/consultar` com o token
no corpo, nunca na URL (o caminho vai para o log de acesso e para o
`Referer`). Token inexistente, vencido, revogado ou de fechamento reaberto e
a flag desligada dão o mesmo 404 "Link inválido ou expirado.". Limite
`LIMITE_RELATORIO_PUBLICO` por IP (o do navegador, repassado pelo front com
o segredo, app/core/rate_limit.py). A resposta, nos dois casos, leva
`Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow` e
`Referrer-Policy: no-referrer`.

`LINK_PUBLICO_HABILITADO` desligada: criar e consultar dão 404; listar e
revogar continuam, para limpar links se a flag for desligada depois de ligada.

Logs só com o id e o prefixo do link, nunca o token nem o hash.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import EmpresaAtiva, exigir_papel, obter_empresa_ativa
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import (
    LIMITE_LINKS_RELATORIO_POR_USUARIO,
    LIMITE_RELATORIO_PUBLICO,
    chave_por_usuario,
    limiter,
)
from app.models import Fechamento, Usuario
from app.models.link_relatorio import LinkRelatorio
from app.services import fechamentos, relatorio_publico
from app.services.travas import travar_mes_compartilhada

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fechamentos", tags=["links-relatorio"])
router_publico = APIRouter(prefix="/relatorios-publicos", tags=["relatorios-publicos"])

obter_admin_ou_gestor = exigir_papel("admin", "gestor")

LIMITE_ATIVOS = 5
VALIDADE_PADRAO_DIAS = 30
VALIDADE_MAXIMA_DIAS = 90
LINK_INVALIDO = "Link inválido ou expirado."
CABECALHOS_PUBLICOS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validade_dias: Annotated[int, Field(ge=1, le=VALIDADE_MAXIMA_DIAS)] = VALIDADE_PADRAO_DIAS


class LinkResponse(BaseModel):
    id: uuid.UUID
    fechamento_id: uuid.UUID
    prefixo: str
    criado_por: str
    criado_em: datetime
    expira_em: datetime
    ultimo_acesso_em: datetime | None
    acessos: int
    revogado_por: str | None
    revogado_em: datetime | None
    motivo_revogacao: Literal["manual", "reabertura"] | None
    ativo: bool


class LinkCriadoResponse(LinkResponse):
    token: str = Field(description="O token do link. Aparece só nesta resposta.")


class LinkListaResponse(BaseModel):
    itens: list[LinkResponse]


class ConsultaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(max_length=200)]


def _erro(status_code: int, detalhe: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detalhe)


def _validar_competencia(competencia: str) -> None:
    if not fechamentos.competencia_valida(competencia):
        raise _erro(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A competência deve estar no formato AAAA-MM, com mês de 01 a 12.",
        )


def _resposta(link: LinkRelatorio, fechamento: Fechamento, agora: datetime) -> dict:
    return {
        "id": link.id,
        "fechamento_id": link.fechamento_id,
        "prefixo": link.prefixo,
        "criado_por": link.criado_por_nome,
        "criado_em": link.criado_em,
        "expira_em": link.expira_em,
        "ultimo_acesso_em": link.ultimo_acesso_em,
        "acessos": link.acessos,
        "revogado_por": link.revogado_por_nome,
        "revogado_em": link.revogado_em,
        "motivo_revogacao": link.motivo_revogacao,
        "ativo": link.revogado_em is None
        and link.expira_em > agora
        and fechamento.reaberto_em is None,
    }


@router.post(
    "/{competencia}/links",
    response_model=LinkCriadoResponse,
    status_code=http_status.HTTP_201_CREATED,
)
@limiter.limit(LIMITE_LINKS_RELATORIO_POR_USUARIO, key_func=chave_por_usuario)
def criar_link(
    request: Request,
    competencia: str,
    _papel: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
    corpo: LinkRequest | None = None,
) -> LinkCriadoResponse:
    if not settings.link_publico_habilitado:
        raise _erro(http_status.HTTP_404_NOT_FOUND, "Link público não disponível.")
    corpo = corpo or LinkRequest()
    usuario, empresa_id = ativa.usuario, ativa.empresa.id
    _validar_competencia(competencia)

    travar_mes_compartilhada(db, empresa_id, competencia)
    fechamento = db.scalar(
        select(Fechamento)
        .where(
            Fechamento.empresa_id == empresa_id,
            Fechamento.competencia == competencia,
            Fechamento.reaberto_em.is_(None),
        )
        .with_for_update()
    )
    if fechamento is None:
        raise _erro(http_status.HTTP_404_NOT_FOUND, "Não há fechamento ativo nesta competência.")
    ativos = db.scalar(
        select(func.count())
        .select_from(LinkRelatorio)
        .where(LinkRelatorio.fechamento_id == fechamento.id, *relatorio_publico.ativo())
    )
    if ativos >= LIMITE_ATIVOS:
        raise _erro(
            http_status.HTTP_409_CONFLICT,
            f"Este fechamento já tem {LIMITE_ATIVOS} links ativos. Revogue um antes de criar "
            "outro.",
        )

    novo = relatorio_publico.gerar_token()
    agora = db.scalar(select(func.now()))
    link = LinkRelatorio(
        empresa_id=empresa_id,
        fechamento_id=fechamento.id,
        prefixo=novo.prefixo,
        hash=novo.hash,
        criado_por_id=usuario.id,
        criado_por_nome=usuario.nome,
        expira_em=agora + timedelta(days=corpo.validade_dias),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    logger.info("link_relatorio resultado=criado link_id=%s prefixo=%s", link.id, link.prefixo)
    return LinkCriadoResponse(**_resposta(link, fechamento, agora), token=novo.token)


@router.get("/{competencia}/links", response_model=LinkListaResponse)
def listar_links(
    competencia: str,
    _papel: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> LinkListaResponse:
    _validar_competencia(competencia)
    agora = db.scalar(select(func.now()))
    linhas = db.execute(
        select(LinkRelatorio, Fechamento)
        .join(Fechamento, Fechamento.id == LinkRelatorio.fechamento_id)
        .where(
            LinkRelatorio.empresa_id == ativa.empresa.id,
            Fechamento.empresa_id == ativa.empresa.id,
            Fechamento.competencia == competencia,
        )
        .order_by(LinkRelatorio.criado_em.desc(), LinkRelatorio.id.desc())
    ).all()
    return LinkListaResponse(
        itens=[_resposta(link, fechamento, agora) for link, fechamento in linhas]
    )


@router.delete("/{competencia}/links/{link_id}", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_LINKS_RELATORIO_POR_USUARIO, key_func=chave_por_usuario)
def revogar_link(
    request: Request,
    competencia: str,
    link_id: uuid.UUID,
    _papel: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _validar_competencia(competencia)
    link = db.scalar(
        select(LinkRelatorio)
        .join(Fechamento, Fechamento.id == LinkRelatorio.fechamento_id)
        .where(
            LinkRelatorio.id == link_id,
            LinkRelatorio.empresa_id == ativa.empresa.id,
            Fechamento.competencia == competencia,
        )
        .with_for_update(of=LinkRelatorio)
    )
    if link is None:
        raise _erro(http_status.HTTP_404_NOT_FOUND, "Link não encontrado.")
    if link.revogado_em is None:
        link.revogado_em = func.clock_timestamp()
        link.revogado_por_id = ativa.usuario.id
        link.revogado_por_nome = ativa.usuario.nome
        link.motivo_revogacao = "manual"
        db.commit()
        logger.info(
            "link_relatorio resultado=revogado link_id=%s prefixo=%s", link.id, link.prefixo
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


def _nao_encontrado() -> JSONResponse:
    return JSONResponse(
        status_code=http_status.HTTP_404_NOT_FOUND,
        content={"detail": LINK_INVALIDO},
        headers=CABECALHOS_PUBLICOS,
    )


@router_publico.post("/consultar")
@limiter.limit(LIMITE_RELATORIO_PUBLICO)
def consultar_relatorio(
    request: Request,
    corpo: ConsultaRequest,
    db: Annotated[Session, Depends(get_db)],
):
    if not settings.link_publico_habilitado:
        return _nao_encontrado()
    encontrado = relatorio_publico.link_valido(db, corpo.token)
    if encontrado is None:
        logger.info("relatorio_publico resultado=invalido")
        return _nao_encontrado()
    link, fechamento = encontrado
    conteudo = relatorio_publico.relatorio(db, link, fechamento)
    relatorio_publico.registrar_acesso(db, link)
    db.commit()
    logger.info("relatorio_publico resultado=lido link_id=%s prefixo=%s", link.id, link.prefixo)
    return JSONResponse(content=conteudo, headers=CABECALHOS_PUBLICOS)
