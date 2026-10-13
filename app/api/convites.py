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

`POST /convites/aceitar/google` (issue #73) é o mesmo aceite com o
`id_token` do Google no lugar da senha. O backend confere o token antes de
tocar no banco (app/core/google.py), e o e-mail do Google tem de ser o do
convite. Cria o usuário sem senha, com o `google_sub`, e responde com
`LoginResponse`, porque já é o login.

Token inexistente, expirado, usado ou cancelado: 400 nas três, nunca 401 (o
frontend trata todo 401 como sessão expirada). Se o e-mail ganhou conta entre
o convite e o aceite: 409.

Nunca loga o token do convite, o `id_token`, o e-mail, o `sub` nem o nome: só
o resultado categorizado.
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

from app.api.auth import (
    CONTA_GOOGLE_JA_CADASTRADA,
    IdToken,
    LoginResponse,
    SenhaNova,
    TextoPreenchido,
    UsuarioResponse,
    nome_da_conta,
    resposta_de_login,
    usuario_pelo_google_sub,
)
from app.core import senha
from app.core.database import get_db
from app.core.google import identidade_ou_erro_http
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


class AceiteConviteGoogleRequest(BaseModel):
    token: Token
    id_token: IdToken
    # Sem `nome`, vale o `name` do token do Google, como no /register/google.
    nome: TextoPreenchido | None = None


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


@router.post(
    "/aceitar/google", response_model=LoginResponse, status_code=http_status.HTTP_201_CREATED
)
@limiter.limit(LIMITE_CONVITE)
def aceitar_com_google(
    request: Request,
    dados: AceiteConviteGoogleRequest,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    # Antes do banco: a busca das chaves não pode segurar o FOR UPDATE.
    identidade = identidade_ou_erro_http(dados.id_token, logger, "convite_aceite_google")
    nome = nome_da_conta(dados.nome, identidade)

    convite = convite_pendente_pelo_token(db, dados.token, travar=True)
    if convite is None:
        logger.info("convite_aceite_google resultado=token_invalido")
        raise _convite_invalido()
    if identidade.email != convite.email:
        logger.info("convite_aceite_google resultado=email_diferente")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="O e-mail da conta Google é diferente do e-mail convidado.",
        )

    email_em_uso = HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="E-mail já cadastrado."
    )
    if db.scalar(select(Usuario.id).where(Usuario.email == convite.email)):
        logger.info("convite_aceite_google resultado=email_em_uso")
        raise email_em_uso
    if usuario_pelo_google_sub(db, identidade.sub) is not None:
        # A conta Google já entra no Ledgr com outro e-mail (trocou o e-mail
        # no Google depois do cadastro).
        logger.info("convite_aceite_google resultado=google_ja_cadastrado")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=CONTA_GOOGLE_JA_CADASTRADA
        )

    usuario = Usuario(
        id=uuid.uuid4(),
        empresa_id=convite.empresa_id,
        nome=nome,
        email=convite.email,
        senha_hash=None,
        google_sub=identidade.sub,
        administrador=False,
    )
    db.add(usuario)
    convite.usado_em = func.now()
    try:
        db.commit()
    except IntegrityError as exc:
        # Corrida com um cadastro ou outro aceite do mesmo e-mail.
        db.rollback()
        logger.info("convite_aceite_google resultado=email_em_uso")
        raise email_em_uso from exc

    logger.info("convite_aceite_google resultado=aceito")
    return resposta_de_login(usuario)
