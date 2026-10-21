"""Cadastro e login por credenciais (issue #57).

O login devolve só os dados do usuário, sem token: quem emite o JWT é o
NextAuth (ADR-003). Desde a issue #75, devolve também o `auth_time` que o
front grava no token: o instante do login no relógio do backend, o mesmo das
comparações da revogação de sessão (app/core/auth.py). O cadastro é self-service, cria a empresa junto, com quem
cadastrou como administrador dela (issue #69), e manda o e-mail de
boas-vindas quando o e-mail está ligado (issue #78), e
`POST /register/verificar` diz antes se o e-mail e o CNPJ estão livres (issue
#80). O 401 do login é o mesmo pra qualquer falha, pra não revelar quais
e-mails têm conta.

Além do rate limit por IP, o login tem bloqueio por conta (issue #67): 5 senhas
erradas para o mesmo e-mail em 15 minutos bloqueiam esse e-mail por 15 minutos,
com 429 e `Retry-After`. Ver app/core/bloqueio_login.py.

Login e cadastro pelo Google (issue #73, adendo de 13/10 do ADR-004):
`POST /login/google` e `POST /register/google` recebem o `id_token` do Google,
que o backend confere sozinho (app/core/google.py) antes de tocar no banco.
O login acha o usuário pelo `google_sub`; sem conta, 404, e o front leva ao
cadastro, que pede CNPJ e razão social como o `/register`. Um e-mail do Google
que já tem conta com senha não é vinculado (409): o cadastro do Ledgr não
confirma e-mail, e vincular entregaria a conta criada antes por outra pessoa
com o e-mail da vítima. As duas respondem com `LoginResponse`, com o
`auth_time` do `instante_do_login`.

Verificação do CNPJ na Receita (issue #119, app/services/consulta_cnpj/): o
`/register` e o `/register/google` consultam (ou usam o cache de 24 horas)
depois das validações locais e antes de gravar, com a transação de leitura já
encerrada, para a chamada externa (até 5 s) não segurar conexão do banco.
"nao_encontrado" e "inativa" dão 422 no formato do FastAPI, com `loc`
`["body", "cnpj"]`; "indisponivel" (consulta desligada, API fora do ar,
lenta ou recusando o formato) não bloqueia, e a empresa nasce não
verificada. Com "ativa", `cnpj_verificado_em` recebe o instante do cadastro;
nada mais da consulta é gravado. O `/register/verificar` devolve a situação
(`cnpj_situacao`) e a razão social da Receita para a tela oferecer, e só
consulta quando o CNPJ ainda não tem conta no Ledgr.

Contas e empresas clientes (issue #133, ADR-019): o cadastro cria a conta e a
primeira empresa cliente dela, com o mesmo CNPJ e a mesma razão social (a
consulta na Receita roda uma vez e vale para as duas), a configuração dessa
empresa e o usuário com papel 'admin'. "CNPJ já cadastrado" olha o CNPJ das
contas, inclusive no `/register/verificar`: o CNPJ da conta é único no
sistema, e o mesmo CNPJ como empresa cliente de outra conta não impede o
cadastro. As respostas trazem `conta_id`, o valor que o front grava no token,
e `empresa_id` com o mesmo valor, obsoleto, até o front migrar.
"""

import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import bloqueio_login, senha
from app.core.auth import instante_do_login, normalizar_email
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.google import IdentidadeGoogle, identidade_ou_erro_http
from app.core.rate_limit import (
    LIMITE_CADASTRO,
    LIMITE_LOGIN,
    LIMITE_VERIFICACAO_CADASTRO,
    limiter,
)
from app.models import Configuracao, Conta, Empresa, Usuario
from app.services.consulta_cnpj import (
    ConsultaCNPJ,
    ResultadoConsultaCNPJ,
    consultar_cnpj,
    obter_consulta_cnpj_dependencia,
)
from app.services.consulta_cnpj.base import INDISPONIVEL
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


# "indisponivel" da consulta vira "nao_verificado" para a tela (issue #119).
SituacaoCnpjNaResposta = Literal["ativa", "inativa", "nao_encontrado", "nao_verificado"]


class VerificacaoCadastroResponse(BaseModel):
    email_disponivel: bool
    cnpj_disponivel: bool
    cnpj_situacao: SituacaoCnpjNaResposta
    # A razão social que a Receita devolveu, só com "ativa" ou "inativa".
    razao_social: str | None


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    senha: str = Field(min_length=1)


class UsuarioResponse(BaseModel):
    id: uuid.UUID
    conta_id: uuid.UUID
    empresa_id: uuid.UUID = Field(
        description="Obsoleto: igual a `conta_id` (issue #133). Use `conta_id`.",
        json_schema_extra={"deprecated": True},
    )
    nome: str
    email: str


def resposta_de_usuario(usuario: Usuario) -> UsuarioResponse:
    return UsuarioResponse(
        id=usuario.id,
        conta_id=usuario.conta_id,
        empresa_id=usuario.conta_id,
        nome=usuario.nome,
        email=usuario.email,
    )


class LoginResponse(UsuarioResponse):
    """Do `/login` e das rotas do Google (issue #73), que também fazem o
    login: o `/register` e o `/convites/aceitar` não mudam."""

    auth_time: int


def resposta_de_login(usuario: Usuario) -> LoginResponse:
    """`LoginResponse` com o `auth_time` de agora (`instante_do_login`)."""
    return LoginResponse(**resposta_de_usuario(usuario).model_dump(), auth_time=instante_do_login())


# Um ID token do Google tem 1 a 2 KB.
IdToken = Annotated[str, Field(min_length=1, max_length=4096)]


def _texto_preenchido(valor: str) -> str:
    valor = valor.strip()
    if not valor:
        raise ValueError("não pode ser vazio")
    return valor


# Texto não vazio, sem espaços nas pontas: a regra de `nome` e `razao_social`.
TextoPreenchido = Annotated[str, Field(min_length=1), AfterValidator(_texto_preenchido)]


class CadastroGoogleRequest(BaseModel):
    id_token: IdToken
    razao_social: TextoPreenchido
    cnpj: CnpjValido
    # Sem `nome`, vale o `name` do token do Google.
    nome: TextoPreenchido | None = None


class LoginGoogleRequest(BaseModel):
    id_token: IdToken


def nome_da_conta(nome_do_corpo: str | None, identidade: IdentidadeGoogle) -> str:
    """O `nome` do corpo ou, sem ele, o `name` do token do Google. Sem os
    dois, 422 no formato do FastAPI, com `loc` no campo `nome`."""
    nome = nome_do_corpo or identidade.nome
    if nome is None:
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "nome"),
                    "msg": "A conta Google não informou o nome: mande o campo nome.",
                    "input": None,
                }
            ]
        )
    return nome


def _conflito(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detalhe)


CONTA_GOOGLE_JA_CADASTRADA = "Já existe uma conta no Ledgr para esta conta Google."

# 422 do cadastro pela situação do CNPJ na Receita (issue #119).
CNPJ_RECUSADO_PELA_RECEITA = {
    "nao_encontrado": "CNPJ não encontrado na Receita Federal.",
    "inativa": "CNPJ não está ativo na Receita Federal.",
}

ConsultaCnpjDependencia = Annotated[ConsultaCNPJ | None, Depends(obter_consulta_cnpj_dependencia)]


def situacao_na_receita(
    db: Session, consulta: ConsultaCNPJ | None, cnpj: str
) -> ResultadoConsultaCNPJ:
    """Consulta (ou cache) do CNPJ, com a transação de leitura encerrada antes:
    a chamada externa não pode segurar uma conexão do banco."""
    db.rollback()
    return consultar_cnpj(cnpj, consulta)


def barrar_cnpj_recusado(resultado: ResultadoConsultaCNPJ, cnpj: str) -> None:
    """422 com "nao_encontrado" ou "inativa", no formato do FastAPI e com
    `loc` no campo `cnpj`, como os outros 422 do cadastro."""
    mensagem = CNPJ_RECUSADO_PELA_RECEITA.get(resultado.situacao)
    if mensagem is not None:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "cnpj"), "msg": mensagem, "input": cnpj}]
        )


def cnpj_verificado_em(resultado: ResultadoConsultaCNPJ) -> datetime | None:
    return datetime.now(UTC) if resultado.situacao == "ativa" else None


def cnpj_de_conta(db: Session, cnpj: str) -> uuid.UUID | None:
    """A conta com este CNPJ, se houver (issue #133: o CNPJ único no sistema é
    o da conta)."""
    return db.scalar(select(Conta.id).where(Conta.cnpj == cnpj))


def nova_conta(
    razao_social: str, cnpj: str, receita: ResultadoConsultaCNPJ
) -> tuple[Conta, Empresa, Configuracao]:
    """A conta, a primeira empresa cliente dela, com o mesmo CNPJ e a mesma
    razão social, e a configuração dessa empresa (issue #133). A consulta na
    Receita vale para as duas."""
    verificado_em = cnpj_verificado_em(receita)
    conta = Conta(
        id=uuid.uuid4(), razao_social=razao_social, cnpj=cnpj, cnpj_verificado_em=verificado_em
    )
    empresa = Empresa(
        id=uuid.uuid4(),
        conta_id=conta.id,
        razao_social=razao_social,
        cnpj=cnpj,
        cnpj_verificado_em=verificado_em,
    )
    return conta, empresa, Configuracao(empresa_id=empresa.id)


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
    consulta: ConsultaCnpjDependencia,
) -> UsuarioResponse:
    email = normalizar_email(dados.email)
    if db.scalar(select(Usuario.id).where(Usuario.email == email)):
        raise _conflito("E-mail já cadastrado.")
    if cnpj_de_conta(db, dados.cnpj):
        raise _conflito("CNPJ já cadastrado.")
    receita = situacao_na_receita(db, consulta, dados.cnpj)
    barrar_cnpj_recusado(receita, dados.cnpj)

    conta, empresa, configuracao = nova_conta(dados.razao_social, dados.cnpj, receita)
    usuario = Usuario(
        conta_id=conta.id,
        nome=dados.nome,
        email=email,
        senha_hash=senha.gerar_hash(dados.senha),
        papel="admin",  # quem cadastra a conta a administra (issues #69 e #133)
    )
    db.add_all([conta, empresa, configuracao, usuario])
    try:
        db.commit()
    except IntegrityError as exc:
        # Corrida entre dois cadastros simultâneos com o mesmo e-mail ou CNPJ.
        db.rollback()
        raise _conflito("E-mail ou CNPJ já cadastrado.") from exc

    agendar_boas_vindas(tarefas, provedor, usuario, dados.razao_social)
    return resposta_de_usuario(usuario)


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
    consulta: ConsultaCnpjDependencia,
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

    Situação na Receita (issue #119): só consulta quando o CNPJ ainda não tem
    conta no Ledgr (o tempo da consulta revela isso, mas `cnpj_disponivel`
    já diz). Sem consulta, ou com ela indisponível, `cnpj_situacao` é
    "nao_verificado". A razão social da Receita só vem com "ativa" ou
    "inativa". O resultado fica 24 horas no cache, e o `/register` em seguida
    não consulta de novo.
    """
    email_em_uso = db.scalar(
        select(Usuario.id).where(Usuario.email == normalizar_email(dados.email))
    )
    cnpj_em_uso = cnpj_de_conta(db, dados.cnpj)
    receita = situacao_na_receita(db, consulta, dados.cnpj) if cnpj_em_uso is None else INDISPONIVEL
    return VerificacaoCadastroResponse(
        email_disponivel=email_em_uso is None,
        cnpj_disponivel=cnpj_em_uso is None,
        cnpj_situacao="nao_verificado" if receita.situacao == "indisponivel" else receita.situacao,
        razao_social=receita.razao_social,
    )


@router.post("/login", response_model=LoginResponse)
@limiter.limit(LIMITE_LOGIN)
def login(
    request: Request,
    dados: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
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

    # Usuário removido (issue #69) cai no mesmo caminho de senha errada: sem
    # hash, o bcrypt roda contra o hash fictício e a resposta é o mesmo 401.
    usuario = db.scalar(
        select(Usuario).where(Usuario.email == email, Usuario.removido_em.is_(None))
    )
    senha_hash = usuario.senha_hash if usuario else None

    if not senha.verificar(dados.senha, senha_hash):
        if bloqueio_login.registrar_falha(email):
            logger.info("login_bloqueado motivo=limite_de_falhas")
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha inválidos.",
        )

    auth_time = instante_do_login()  # depois de a senha ser conferida
    bloqueio_login.limpar(email)
    return LoginResponse(**resposta_de_usuario(usuario).model_dump(), auth_time=auth_time)


def usuario_pelo_google_sub(db: Session, sub: str) -> Usuario | None:
    # O removido não tem `google_sub` (issue #69): nunca é encontrado.
    return db.scalar(
        select(Usuario).where(Usuario.google_sub == sub, Usuario.removido_em.is_(None))
    )


@router.post("/login/google", response_model=LoginResponse)
@limiter.limit(LIMITE_LOGIN)
def login_google(
    request: Request,
    dados: LoginGoogleRequest,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    """Login pelo Google (issue #73). 404 sem conta, para o front levar ao
    cadastro; 409 quando o e-mail já tem conta com senha (sem vínculo
    automático). Não loga o token, o e-mail nem o `sub`."""
    # Antes do banco: a busca das chaves não pode segurar uma transação.
    identidade = identidade_ou_erro_http(dados.id_token, logger, "login_google")

    usuario = usuario_pelo_google_sub(db, identidade.sub)
    if usuario is not None:
        logger.info("login_google resultado=entrou")
        return resposta_de_login(usuario)

    if db.scalar(select(Usuario.id).where(Usuario.email == identidade.email)):
        logger.info("login_google resultado=email_com_outra_conta")
        raise _conflito("Já existe uma conta com este e-mail. Entre com e-mail e senha.")
    logger.info("login_google resultado=sem_conta")
    raise HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND,
        detail="Não há conta no Ledgr para esta conta Google.",
    )


@router.post(
    "/register/google",
    response_model=LoginResponse,
    status_code=http_status.HTTP_201_CREATED,
)
@limiter.limit(LIMITE_CADASTRO)
def cadastrar_google(
    request: Request,
    dados: CadastroGoogleRequest,
    tarefas: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
    consulta: ConsultaCnpjDependencia,
) -> LoginResponse:
    """Cadastro pelo Google (issue #73): como o `/register`, com o e-mail e o
    `google_sub` do token e sem senha. Quem cadastra é o admin da conta, e
    as boas-vindas saem do mesmo jeito. Responde com
    `LoginResponse`, porque já é o login."""
    # Antes do banco: a busca das chaves não pode segurar uma transação.
    identidade = identidade_ou_erro_http(dados.id_token, logger, "cadastro_google")
    nome = nome_da_conta(dados.nome, identidade)

    if usuario_pelo_google_sub(db, identidade.sub) is not None:
        logger.info("cadastro_google resultado=google_ja_cadastrado")
        raise _conflito(CONTA_GOOGLE_JA_CADASTRADA)
    if db.scalar(select(Usuario.id).where(Usuario.email == identidade.email)):
        logger.info("cadastro_google resultado=email_em_uso")
        raise _conflito("E-mail já cadastrado.")
    if cnpj_de_conta(db, dados.cnpj):
        logger.info("cadastro_google resultado=cnpj_em_uso")
        raise _conflito("CNPJ já cadastrado.")
    receita = situacao_na_receita(db, consulta, dados.cnpj)
    if receita.situacao in CNPJ_RECUSADO_PELA_RECEITA:
        logger.info("cadastro_google resultado=cnpj_%s", receita.situacao)
    barrar_cnpj_recusado(receita, dados.cnpj)

    conta, empresa, configuracao = nova_conta(dados.razao_social, dados.cnpj, receita)
    usuario = Usuario(
        conta_id=conta.id,
        nome=nome,
        email=identidade.email,
        senha_hash=None,
        google_sub=identidade.sub,
        papel="admin",  # quem cadastra a conta a administra (issues #69 e #133)
    )
    db.add_all([conta, empresa, configuracao, usuario])
    try:
        db.commit()
    except IntegrityError as exc:
        # Corrida com outro cadastro do mesmo e-mail, CNPJ ou conta Google.
        db.rollback()
        logger.info("cadastro_google resultado=conflito_concorrente")
        raise _conflito("E-mail ou CNPJ já cadastrado.") from exc

    logger.info("cadastro_google resultado=cadastrado")
    agendar_boas_vindas(tarefas, provedor, usuario, dados.razao_social)
    return resposta_de_login(usuario)
