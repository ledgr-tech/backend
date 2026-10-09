"""Cadastro e login por credenciais (issue #57).

O login devolve só os dados do usuário, sem token: quem emite o JWT é o
NextAuth (ADR-003). O cadastro é self-service, cria a empresa junto e manda
o e-mail de boas-vindas quando o e-mail está ligado (issue #78), e
`POST /register/verificar` diz antes se o e-mail e o CNPJ estão livres (issue
#80). O 401 do login é o mesmo pra qualquer falha, pra não revelar quais
e-mails têm conta.

Além do rate limit por IP, o login tem bloqueio por conta (issue #67): 5 senhas
erradas para o mesmo e-mail em 15 minutos bloqueiam esse e-mail por 15 minutos,
com 429 e `Retry-After`. Ver app/core/bloqueio_login.py.
"""

import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import bloqueio_login, senha
from app.core.auth import normalizar_email
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import (
    LIMITE_CADASTRO,
    LIMITE_LOGIN,
    LIMITE_VERIFICACAO_CADASTRO,
    limiter,
)
from app.models import Configuracao, Empresa, Usuario
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_boas_vindas

logger = logging.getLogger(__name__)

router = APIRouter(tags=["autenticacao"])

TAMANHO_MINIMO_SENHA = 8


def _senha_cabe_no_bcrypt(valor: str) -> str:
    if len(valor.encode()) > senha.TAMANHO_MAXIMO_BYTES:
        raise ValueError(f"não pode passar de {senha.TAMANHO_MAXIMO_BYTES} bytes")
    return valor


# Regra de senha nova, a mesma no cadastro e na redefinição (issue #66).
SenhaNova = Annotated[
    str, Field(min_length=TAMANHO_MINIMO_SENHA), AfterValidator(_senha_cabe_no_bcrypt)
]


def _cnpj_valido(valor: str) -> str:
    normalizado = normalizar_cnpj(valor)
    if normalizado is None:
        raise ValueError("CNPJ inválido")
    return normalizado


# CNPJ com dígito verificador, sem máscara e em maiúsculas; o mesmo no cadastro
# e na verificação antes dele (issue #80).
CnpjValido = Annotated[str, AfterValidator(_cnpj_valido)]


class CadastroRequest(BaseModel):
    nome: str = Field(min_length=1)
    email: EmailStr
    senha: SenhaNova
    razao_social: str = Field(min_length=1)
    cnpj: CnpjValido

    @field_validator("nome", "razao_social")
    @classmethod
    def _sem_espacos_nas_pontas(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("não pode ser vazio")
        return valor


class VerificacaoCadastroRequest(BaseModel):
    email: EmailStr
    cnpj: CnpjValido


class VerificacaoCadastroResponse(BaseModel):
    email_disponivel: bool
    cnpj_disponivel: bool


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
    tarefas: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> UsuarioResponse:
    email = normalizar_email(dados.email)
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

    agendar_boas_vindas(tarefas, provedor, usuario, dados.razao_social)
    return UsuarioResponse(
        id=usuario.id, empresa_id=empresa.id, nome=usuario.nome, email=usuario.email
    )


def agendar_boas_vindas(
    tarefas: BackgroundTasks,
    provedor: ProvedorDeEmail | None,
    usuario: Usuario,
    razao_social: str,
) -> None:
    """E-mail de boas-vindas depois do cadastro (issue #78).

    Sai em `BackgroundTasks`, depois da resposta: nenhuma falha de envio muda o
    201. Com o e-mail desligado, o cadastro vale do mesmo jeito, sem e-mail.
    Com `FRONTEND_URL` recusada (app/core/frontend_url.py), vai sem o link de
    login, como o aviso de senha alterada. A chave de idempotência é por
    usuário: um reenvio do Resend com a mesma chave não duplica o e-mail.
    """
    if provedor is None:
        logger.info("boas_vindas resultado=desabilitado")
        return
    base = frontend_url_para_link()
    mensagem = mensagem_boas_vindas(
        para=usuario.email,
        nome=usuario.nome,
        razao_social=razao_social,
        link_login=f"{base}/login" if base else None,
        chave_idempotencia=f"boas-vindas-{usuario.id}",
    )
    tarefas.add_task(enviar_e_registrar, provedor, mensagem, "boas_vindas")


@router.post("/register/verificar", response_model=VerificacaoCadastroResponse)
@limiter.limit(LIMITE_VERIFICACAO_CADASTRO)
def verificar_cadastro(
    request: Request,
    dados: VerificacaoCadastroRequest,
    db: Annotated[Session, Depends(get_db)],
) -> VerificacaoCadastroResponse:
    """Diz se o e-mail e o CNPJ ainda estão livres, antes do fim do cadastro (issue #80).

    Pública, como o /register. Valida os dois campos com as mesmas regras dele
    (inválido é 422 no mesmo formato) e não cria nada: o /register confere tudo
    de novo no fim, e a corrida entre verificar e cadastrar continua coberta pelo
    409 dele.

    A resposta diz qual dos dois está em uso, de propósito, divergindo do texto
    da issue. Uma resposta genérica ("um dos dois já existe") não impediria a
    enumeração: basta mandar o e-mail com um CNPJ válido inventado. E o próprio
    /register já responde "E-mail já cadastrado." ou "CNPJ já cadastrado."
    separadamente. O que limita a enumeração é o rate limit por IP
    (`LIMITE_VERIFICACAO_CADASTRO`).

    As duas consultas rodam sempre, mesmo quando a primeira já achou, para o
    tempo de resposta não depender de qual dos dois existe. Não loga o e-mail
    nem o CNPJ.
    """
    email_em_uso = db.scalar(
        select(Usuario.id).where(Usuario.email == normalizar_email(dados.email))
    )
    cnpj_em_uso = db.scalar(select(Empresa.id).where(Empresa.cnpj == dados.cnpj))
    return VerificacaoCadastroResponse(
        email_disponivel=email_em_uso is None, cnpj_disponivel=cnpj_em_uso is None
    )


@router.post("/login", response_model=UsuarioResponse)
@limiter.limit(LIMITE_LOGIN)
def login(
    request: Request,
    dados: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioResponse:
    email = normalizar_email(dados.email)

    # Bloqueio por conta (issue #67): antes do banco e do bcrypt, igual para
    # e-mail com e sem conta. Não loga o e-mail nem o hash dele.
    ate = bloqueio_login.bloqueado_ate(email)
    if ate is not None:
        logger.info("login_bloqueado motivo=tentativa_durante_bloqueio")
        segundos = max(1, math.ceil((ate - datetime.now(UTC)).total_seconds()))
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Tente novamente mais tarde.",
            headers={"Retry-After": str(segundos)},
        )

    usuario = db.scalar(select(Usuario).where(Usuario.email == email))
    senha_hash = usuario.senha_hash if usuario else None

    if not senha.verificar(dados.senha, senha_hash):
        if bloqueio_login.registrar_falha(email):
            logger.info("login_bloqueado motivo=limite_de_falhas")
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha inválidos.",
        )

    bloqueio_login.limpar(email)
    return UsuarioResponse(
        id=usuario.id, empresa_id=usuario.empresa_id, nome=usuario.nome, email=usuario.email
    )
