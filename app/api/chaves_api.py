"""Chaves da API pública, pelo app (issue #135, ADR-021).

Rotas da conta, com o JWT do app: ignoram `X-Ledgr-Empresa`. Só o admin e o
gestor (`exigir_papel`); o analista recebe 403.

- `GET /chaves-api`: as chaves da conta, ativas e revogadas, das mais novas
  para as mais antigas. Nunca o token nem o hash.
- `POST /chaves-api` (`nome` e, opcional, `empresa_id`): cria a chave e
  devolve o token em `chave`, só nesta resposta. Sem `empresa_id`, a chave
  enxerga todas as empresas da conta; com ele, só aquela (de outra conta ou
  inexistente: 404 "Empresa não encontrada."). No máximo
  `MAXIMO_CHAVES_ATIVAS` ativas por conta (409), contadas com a linha da conta
  travada, para duas criações simultâneas não passarem as duas.
- `DELETE /chaves-api/{id}`: revoga na hora (204). Chave de outra conta: 404;
  já revogada: 204 sem mudar nada.

O token é "ledgr_" seguido de `secrets.token_urlsafe(32)`; só o sha256 dele é
gravado. A regra de plano fica em `app.core.plano.api_publica_liberada`, que
no MVP libera todos. Logs só com o id e o prefixo da chave.
"""

import logging
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import PREFIXO_CHAVE_API, exigir_papel, hash_da_chave_api
from app.core.database import get_db
from app.core.plano import api_publica_liberada
from app.models import ChaveApi, Conta, Empresa, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chaves-api", tags=["chaves-api"])

MAXIMO_CHAVES_ATIVAS = 10
TAMANHO_PREFIXO = 8

obter_admin_ou_gestor = exigir_papel("admin", "gestor")


class EscopoConta(BaseModel):
    tipo: Literal["conta"] = "conta"


class EscopoEmpresa(BaseModel):
    tipo: Literal["empresa"] = "empresa"
    empresa_id: uuid.UUID
    razao_social: str


class ChaveApiResponse(BaseModel):
    id: uuid.UUID
    nome: str
    prefixo: str
    escopo: EscopoConta | EscopoEmpresa
    criada_por: str | None
    criado_em: datetime
    ultimo_uso_em: datetime | None
    revogada_em: datetime | None


class ChaveApiCriadaResponse(ChaveApiResponse):
    chave: str = Field(description="O token da chave. Aparece só nesta resposta.")


class ChaveApiListaResponse(BaseModel):
    itens: list[ChaveApiResponse]


class ChaveApiRequest(BaseModel):
    nome: str
    empresa_id: uuid.UUID | None = None

    @field_validator("nome")
    @classmethod
    def _nome(cls, valor: str) -> str:
        valor = valor.strip()
        if not 1 <= len(valor) <= 60:
            raise ValueError("deve ter de 1 a 60 caracteres")
        return valor


def _consulta_chaves():
    criador = aliased(Usuario)
    return (
        select(ChaveApi, Empresa.razao_social, criador.nome)
        .outerjoin(Empresa, Empresa.id == ChaveApi.empresa_id)
        .outerjoin(criador, criador.id == ChaveApi.criada_por_id)
    )


def _resposta(chave: ChaveApi, razao_social: str | None, criada_por: str | None) -> dict:
    escopo = (
        EscopoConta()
        if chave.empresa_id is None
        else EscopoEmpresa(empresa_id=chave.empresa_id, razao_social=razao_social)
    )
    return {
        "id": chave.id,
        "nome": chave.nome,
        "prefixo": chave.prefixo,
        "escopo": escopo,
        "criada_por": criada_por,
        "criado_em": chave.criado_em,
        "ultimo_uso_em": chave.ultimo_uso_em,
        "revogada_em": chave.revogada_em,
    }


@router.get("", response_model=ChaveApiListaResponse)
def listar_chaves(
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> ChaveApiListaResponse:
    linhas = db.execute(
        _consulta_chaves()
        .where(ChaveApi.conta_id == usuario.conta_id)
        .order_by(ChaveApi.criado_em.desc(), ChaveApi.id)
    ).all()
    return ChaveApiListaResponse(itens=[ChaveApiResponse(**_resposta(*linha)) for linha in linhas])


@router.post("", response_model=ChaveApiCriadaResponse, status_code=http_status.HTTP_201_CREATED)
def criar_chave(
    dados: ChaveApiRequest,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> ChaveApiCriadaResponse:
    conta_id = usuario.conta_id
    conta = db.scalar(select(Conta).where(Conta.id == conta_id).with_for_update())
    if not api_publica_liberada(conta):  # sempre liberada no MVP
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail="O plano da sua conta não inclui a API pública.",
        )

    razao_social = None
    if dados.empresa_id is not None:
        empresa = db.get(Empresa, dados.empresa_id)
        if empresa is None or empresa.conta_id != conta_id:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
            )
        razao_social = empresa.razao_social

    ativas = db.scalar(
        select(func.count()).where(ChaveApi.conta_id == conta_id, ChaveApi.revogada_em.is_(None))
    )
    if ativas >= MAXIMO_CHAVES_ATIVAS:
        logger.info("chave_api resultado=limite_de_ativas")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"Sua conta já tem {MAXIMO_CHAVES_ATIVAS} chaves ativas. "
                "Revogue uma antes de criar outra."
            ),
        )

    aleatorio = secrets.token_urlsafe(32)
    token = f"{PREFIXO_CHAVE_API}{aleatorio}"
    chave = ChaveApi(
        id=uuid.uuid4(),
        conta_id=conta_id,
        empresa_id=dados.empresa_id,
        nome=dados.nome,
        prefixo=aleatorio[:TAMANHO_PREFIXO],
        hash=hash_da_chave_api(token),
        criada_por_id=usuario.id,
    )
    db.add(chave)
    nome_de_quem_criou = usuario.nome
    db.commit()
    db.refresh(chave)
    logger.info("chave_api resultado=criada chave_id=%s prefixo=%s", chave.id, chave.prefixo)
    return ChaveApiCriadaResponse(**_resposta(chave, razao_social, nome_de_quem_criou), chave=token)


@router.delete("/{chave_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def revogar_chave(
    chave_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    chave = db.scalar(
        select(ChaveApi)
        .where(ChaveApi.id == chave_id, ChaveApi.conta_id == usuario.conta_id)
        .with_for_update()
    )
    if chave is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Chave não encontrada."
        )
    if chave.revogada_em is not None:
        db.rollback()
        return
    chave.revogada_em = func.now()
    chave.revogada_por_id = usuario.id
    db.commit()
    logger.info("chave_api resultado=revogada chave_id=%s prefixo=%s", chave.id, chave.prefixo)
