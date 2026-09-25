"""Cadastro e login por credenciais (issue #57).

O login devolve só os dados do usuário, sem token: quem emite o JWT é o
NextAuth (ADR-003). O cadastro é self-service e cria a empresa junto. O 401 do
login é o mesmo pra qualquer falha, pra não revelar quais e-mails têm conta.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import senha
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.database import get_db
from app.core.rate_limit import LIMITE_CADASTRO, LIMITE_LOGIN, limiter
from app.models import Configuracao, Empresa, Usuario

router = APIRouter(tags=["autenticacao"])

TAMANHO_MINIMO_SENHA = 8


def _normalizar_email(email: str) -> str:
    return email.strip().lower()


class CadastroRequest(BaseModel):
    nome: str = Field(min_length=1)
    email: EmailStr
    senha: str = Field(min_length=TAMANHO_MINIMO_SENHA)
    razao_social: str = Field(min_length=1)
    cnpj: str

    @field_validator("nome", "razao_social")
    @classmethod
    def _sem_espacos_nas_pontas(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("não pode ser vazio")
        return valor

    @field_validator("senha")
    @classmethod
    def _senha_cabe_no_bcrypt(cls, valor: str) -> str:
        if len(valor.encode()) > senha.TAMANHO_MAXIMO_BYTES:
            raise ValueError(f"não pode passar de {senha.TAMANHO_MAXIMO_BYTES} bytes")
        return valor

    @field_validator("cnpj")
    @classmethod
    def _cnpj_valido(cls, valor: str) -> str:
        normalizado = normalizar_cnpj(valor)
        if normalizado is None:
            raise ValueError("CNPJ inválido")
        return normalizado


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    senha: str = Field(min_length=1)


class UsuarioResponse(BaseModel):
    id: uuid.UUID
    empresa_id: uuid.UUID
    nome: str
    email: str


def _conflito(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detalhe)


@router.post(
    "/register",
    response_model=UsuarioResponse,
    status_code=http_status.HTTP_201_CREATED,
)
@limiter.limit(LIMITE_CADASTRO)
def cadastrar(
    request: Request,
    dados: CadastroRequest,
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioResponse:
    email = _normalizar_email(dados.email)
    if db.scalar(select(Usuario.id).where(Usuario.email == email)):
        raise _conflito("E-mail já cadastrado.")
    if db.scalar(select(Empresa.id).where(Empresa.cnpj == dados.cnpj)):
        raise _conflito("CNPJ já cadastrado.")

    empresa = Empresa(id=uuid.uuid4(), razao_social=dados.razao_social, cnpj=dados.cnpj)
    usuario = Usuario(
        empresa_id=empresa.id,
        nome=dados.nome,
        email=email,
        senha_hash=senha.gerar_hash(dados.senha),
    )
    db.add_all([empresa, Configuracao(empresa_id=empresa.id), usuario])
    try:
        db.commit()
    except IntegrityError as exc:
        # Corrida entre dois cadastros simultâneos com o mesmo e-mail ou CNPJ.
        db.rollback()
        raise _conflito("E-mail ou CNPJ já cadastrado.") from exc

    return UsuarioResponse(
        id=usuario.id, empresa_id=empresa.id, nome=usuario.nome, email=usuario.email
    )


@router.post("/login", response_model=UsuarioResponse)
@limiter.limit(LIMITE_LOGIN)
def login(
    request: Request,
    dados: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioResponse:
    usuario = db.scalar(select(Usuario).where(Usuario.email == _normalizar_email(dados.email)))
    senha_hash = usuario.senha_hash if usuario else None

    if not senha.verificar(dados.senha, senha_hash):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha inválidos.",
        )

    return UsuarioResponse(
        id=usuario.id, empresa_id=usuario.empresa_id, nome=usuario.nome, email=usuario.email
    )
