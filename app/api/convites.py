"""Consulta e aceite de convite, sem login (issue #69, ADR-015).

A página `/aceitar-convite` do front lê o token do fragmento do link e chama
as duas rotas pelo corpo, nunca pela URL. Rate limit por IP igual ao do
`/register` (`LIMITE_CONVITE`).

`POST /convites/consultar` devolve o e-mail convidado, a razão social e a
validade, para a página mostrar quem está sendo convidado e para onde. Não
consome o convite.

`POST /convites/aceitar` cria o usuário (não administrador) na empresa do
convite, com o e-mail do convite, e marca o convite como usado. Responde como
o `/register` (`UsuarioResponse`), e o front faz o login em seguida. Não manda
boas-vindas: quem convidou já avisou.

Token inexistente, expirado, usado ou cancelado: 400 nas duas, nunca 401 (o
frontend trata todo 401 como sessão expirada). Se o e-mail ganhou conta entre
o convite e o aceite: 409.

Nunca loga o token, o e-mail nem o nome: só o resultado categorizado.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import SenhaNova, UsuarioResponse
from app.core import senha
from app.core.database import get_db
from app.core.rate_limit import LIMITE_CONVITE, limiter
from app.models import Empresa, Usuario
from app.services.convites import convite_pendente_pelo_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/convites", tags=["convites"])

Token = Annotated[str, Field(min_length=1, max_length=256)]


class ConsultaConviteRequest(BaseModel):
    token: Token


class ConsultaConviteResponse(BaseModel):
    email: str
    razao_social: str
    expira_em: datetime


class AceiteConviteRequest(BaseModel):
    token: Token
    nome: str = Field(min_length=1)
    senha: SenhaNova

    @field_validator("nome")
    @classmethod
    def _sem_espacos_nas_pontas(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("não pode ser vazio")
        return valor


def _convite_invalido() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail="Convite inválido ou expirado. Peça um novo convite.",
    )


@router.post("/consultar", response_model=ConsultaConviteResponse)
@limiter.limit(LIMITE_CONVITE)
def consultar(
    request: Request,
    dados: ConsultaConviteRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ConsultaConviteResponse:
    convite = convite_pendente_pelo_token(db, dados.token, travar=False)
    if convite is None:
        logger.info("convite_consulta resultado=token_invalido")
        raise _convite_invalido()
    razao_social = db.scalar(select(Empresa.razao_social).where(Empresa.id == convite.empresa_id))
    return ConsultaConviteResponse(
        email=convite.email, razao_social=razao_social, expira_em=convite.expira_em
    )


@router.post("/aceitar", response_model=UsuarioResponse, status_code=http_status.HTTP_201_CREATED)
@limiter.limit(LIMITE_CONVITE)
def aceitar(
    request: Request,
    dados: AceiteConviteRequest,
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioResponse:
    convite = convite_pendente_pelo_token(db, dados.token, travar=True)
    if convite is None:
        logger.info("convite_aceite resultado=token_invalido")
        raise _convite_invalido()

    email_em_uso = HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="E-mail já cadastrado."
    )
    if db.scalar(select(Usuario.id).where(Usuario.email == convite.email)):
        logger.info("convite_aceite resultado=email_em_uso")
        raise email_em_uso

    usuario = Usuario(
        id=uuid.uuid4(),
        empresa_id=convite.empresa_id,
        nome=dados.nome,
        email=convite.email,
        senha_hash=senha.gerar_hash(dados.senha),
        administrador=False,
    )
    db.add(usuario)
    convite.usado_em = func.now()
    resposta = UsuarioResponse(
        id=usuario.id, empresa_id=usuario.empresa_id, nome=usuario.nome, email=usuario.email
    )
    try:
        db.commit()
    except IntegrityError as exc:
        # Corrida com um cadastro ou outro aceite do mesmo e-mail.
        db.rollback()
        logger.info("convite_aceite resultado=email_em_uso")
        raise email_em_uso from exc

    logger.info("convite_aceite resultado=aceito")
    return resposta
