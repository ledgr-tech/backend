"""Troca do e-mail da conta (issue #149; ADR-012, adendo de 22/10).

`POST /me/email` pede a troca, com `email_novo` e `senha_atual`. É rota da
conta (ignora `X-Ledgr-Empresa`), para todos os papéis. As recusas, nesta
ordem:

- conta só do Google (sem senha): 409, porque o e-mail dela é o da conta
  Google, e o login pelo Google não usa o e-mail da Ledgr;
- senha atual errada ou ausente: 400 "Senha atual incorreta.", nunca 401,
  como no `POST /me/senha` (o front trata todo 401 como sessão expirada);
- `email_novo` inválido: 422 no formato do FastAPI; igual ao atual, depois
  de normalizar: 422 "O e-mail novo é igual ao atual.". A validação fica
  dentro da rota, e não no modelo, para vir depois da senha;
- e-mail indisponível (desligado, sem chave, sem remetente ou `FRONTEND_URL`
  recusada): 503, sem gravar nada, como no convite e na recuperação;
- mais de `LIMITE_PEDIDOS_POR_HORA` pedidos do usuário na última hora,
  contados na tabela de tokens: 429 com `Retry-After`;
- `email_novo` já é de outro usuário: 409 "E-mail já cadastrado.". O e-mail
  é único na tabela inteira (o removido fica com `removido+<id>@...`), então
  isso vale para qualquer conta.

O sucesso é 202 com `email_novo` e `expira_em`, e o e-mail da conta NÃO muda
ainda. Um pedido novo cancela os tokens de troca pendentes do usuário. Depois
do commit, em `BackgroundTasks`, o link de confirmação vai para o e-mail novo
(`<FRONTEND_URL>/confirmar-email#token=...`, no fragmento, como o da
recuperação) e um aviso vai para o e-mail atual, com o novo mascarado.

`POST /me/email/confirmar` recebe o token do link. É pública, sem sessão: o
link pode abrir noutro navegador. Rate limit por IP, como a redefinição de
senha. Token inexistente, expirado, usado, cancelado, de outra finalidade ou
de usuário removido: 400 "Link inválido ou expirado.". O token é lido com
`FOR UPDATE`; se o e-mail novo passou a ser de outro usuário entre o pedido e
a confirmação, 409 "E-mail já cadastrado." e o token é marcado como usado.
Senão, o e-mail muda, todos os tokens pendentes do usuário morrem (inclusive
os de recuperação de senha, que foram para o e-mail antigo) e as sessões são
encerradas (issue #75): a pessoa entra de novo com o e-mail novo. Depois do
commit, o aviso de troca concluída vai para o e-mail antigo.

Tokens: `secrets.token_urlsafe(32)`, só o sha256 gravado, uso único,
`VALIDADE_TOKEN_MINUTOS` de validade (ver app/models/token_email.py). Nunca
loga e-mail, token nem link: só `troca_email resultado=...`.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import senha
from app.core.auth import encerrar_sessoes, normalizar_email, obter_usuario_autenticado
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import (
    LIMITE_CONFIRMACAO_TROCA_EMAIL,
    LIMITE_TROCA_EMAIL_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import TokenEmail, Usuario
from app.models.token_email import FINALIDADE_TROCA_EMAIL
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import (
    mensagem_confirmacao_troca_email,
    mensagem_troca_email_concluida,
    mensagem_troca_email_pedida,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me/email", tags=["conta"])

VALIDADE_TOKEN_MINUTOS = 60
LIMITE_PEDIDOS_POR_HORA = 3

CONTA_DO_GOOGLE = "Esta conta entra pelo Google. O e-mail dela é o da conta Google."
SENHA_INCORRETA = "Senha atual incorreta."
EMAIL_IGUAL = "O e-mail novo é igual ao atual."
INDISPONIVEL = "Troca de e-mail indisponível no momento."
LIMITE_EXCEDIDO = "Você já pediu a troca de e-mail várias vezes na última hora. Tente mais tarde."
EMAIL_EM_USO = "E-mail já cadastrado."
LINK_INVALIDO = "Link inválido ou expirado."

_validar_email = TypeAdapter(EmailStr)


class PedidoTrocaEmailRequest(BaseModel):
    # Validado na rota, depois da senha (ver o docstring do módulo).
    email_novo: str = Field(max_length=320)
    # Sem `min_length`: ausente ou vazia é senha incorreta (400), não 422.
    senha_atual: str | None = None


class PedidoTrocaEmailResponse(BaseModel):
    email_novo: str
    expira_em: datetime


class ConfirmacaoTrocaEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ConfirmacaoTrocaEmailResponse(BaseModel):
    email: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _email_valido(valor: str) -> str:
    try:
        return normalizar_email(_validar_email.validate_python(valor.strip()))
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**erro, "loc": ("body", "email_novo")}
                for erro in exc.errors(include_url=False, include_context=False)
            ]
        ) from exc


def _email_em_uso(db: Session, email: str, usuario_id: uuid.UUID) -> bool:
    return (
        db.scalar(select(Usuario.id).where(Usuario.email == email, Usuario.id != usuario_id))
        is not None
    )


def _cancelar_pendentes(db: Session, usuario_id: uuid.UUID, finalidade: str | None) -> None:
    """Marca como usados os tokens pendentes do usuário, de uma finalidade ou
    de todas (`None`)."""
    condicoes = [TokenEmail.usuario_id == usuario_id, TokenEmail.usado_em.is_(None)]
    if finalidade is not None:
        condicoes.append(TokenEmail.finalidade == finalidade)
    db.execute(update(TokenEmail).where(*condicoes).values(usado_em=func.now()))


def _recusar(status_http: int, detalhe: str, resultado: str, **cabecalhos: str) -> HTTPException:
    logger.info("troca_email resultado=%s", resultado)
    return HTTPException(status_code=status_http, detail=detalhe, headers=cabecalhos or None)


@router.post(
    "",
    response_model=PedidoTrocaEmailResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
)
@limiter.limit(LIMITE_TROCA_EMAIL_POR_USUARIO, key_func=chave_por_usuario)
def pedir_troca_de_email(
    request: Request,
    dados: PedidoTrocaEmailRequest,
    tarefas: BackgroundTasks,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> PedidoTrocaEmailResponse:
    if usuario.senha_hash is None:
        raise _recusar(http_status.HTTP_409_CONFLICT, CONTA_DO_GOOGLE, "conta_google")
    if not dados.senha_atual or not senha.verificar(dados.senha_atual, usuario.senha_hash):
        raise _recusar(http_status.HTTP_400_BAD_REQUEST, SENHA_INCORRETA, "senha_incorreta")
    email_novo = _email_valido(dados.email_novo)
    if email_novo == usuario.email:
        raise _recusar(http_status.HTTP_422_UNPROCESSABLE_CONTENT, EMAIL_IGUAL, "email_igual")
    frontend_url = frontend_url_para_link()
    if provedor is None or not frontend_url:
        raise _recusar(http_status.HTTP_503_SERVICE_UNAVAILABLE, INDISPONIVEL, "indisponivel")

    # FOR UPDATE na linha do usuário: dois pedidos simultâneos não passam os
    # dois pela contagem, nem deixam dois links válidos.
    db.refresh(usuario, with_for_update=True)
    uma_hora = timedelta(hours=1)
    pedidos, segundos_ate_liberar = db.execute(
        select(
            func.count(),
            func.ceil(
                func.extract("epoch", func.min(TokenEmail.criado_em) + uma_hora - func.now())
            ),
        ).where(
            TokenEmail.usuario_id == usuario.id,
            TokenEmail.finalidade == FINALIDADE_TROCA_EMAIL,
            TokenEmail.criado_em >= func.now() - uma_hora,
        )
    ).one()
    if pedidos >= LIMITE_PEDIDOS_POR_HORA:
        db.rollback()
        raise _recusar(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            LIMITE_EXCEDIDO,
            "limite_por_hora",
            **{"Retry-After": str(max(1, int(segundos_ate_liberar or 1)))},
        )
    if _email_em_uso(db, email_novo, usuario.id):
        db.rollback()
        raise _recusar(http_status.HTTP_409_CONFLICT, EMAIL_EM_USO, "email_em_uso")

    _cancelar_pendentes(db, usuario.id, FINALIDADE_TROCA_EMAIL)
    token = secrets.token_urlsafe(32)
    registro = TokenEmail(
        id=uuid.uuid4(),
        usuario_id=usuario.id,
        finalidade=FINALIDADE_TROCA_EMAIL,
        token_hash=_hash_token(token),
        expira_em=func.now() + timedelta(minutes=VALIDADE_TOKEN_MINUTOS),
        email_novo=email_novo,
    )
    db.add(registro)
    db.flush()
    db.refresh(registro)
    expira_em = registro.expira_em
    # Antes do commit, que expira os objetos.
    link = mensagem_confirmacao_troca_email(
        para=email_novo,
        nome=usuario.nome,
        link=f"{frontend_url.rstrip('/')}/confirmar-email#token={token}",
        validade_minutos=VALIDADE_TOKEN_MINUTOS,
        chave_idempotencia=f"troca-email/{registro.id}",
    )
    aviso = mensagem_troca_email_pedida(
        para=usuario.email,
        nome=usuario.nome,
        email_novo=email_novo,
        chave_idempotencia=f"troca-email-pedida/{registro.id}",
    )
    db.commit()
    logger.info("troca_email resultado=pedido")

    tarefas.add_task(enviar_e_registrar, provedor, link, "troca_email_link")
    tarefas.add_task(enviar_e_registrar, provedor, aviso, "aviso_troca_email_pedida")
    return PedidoTrocaEmailResponse(email_novo=email_novo, expira_em=expira_em)


@router.post("/confirmar", response_model=ConfirmacaoTrocaEmailResponse)
@limiter.limit(LIMITE_CONFIRMACAO_TROCA_EMAIL)
def confirmar_troca_de_email(
    request: Request,
    dados: ConfirmacaoTrocaEmailRequest,
    tarefas: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> ConfirmacaoTrocaEmailResponse:
    # FOR UPDATE: dois usos simultâneos do mesmo link não passam os dois. O
    # segundo espera e não acha mais o token pendente.
    registro = db.scalar(
        select(TokenEmail)
        .where(
            TokenEmail.token_hash == _hash_token(dados.token),
            TokenEmail.finalidade == FINALIDADE_TROCA_EMAIL,
            TokenEmail.usado_em.is_(None),
            TokenEmail.expira_em > func.now(),
        )
        .with_for_update()
    )
    usuario = db.get(Usuario, registro.usuario_id, with_for_update=True) if registro else None
    if usuario is None or usuario.removido_em is not None:
        db.rollback()
        raise _recusar(http_status.HTTP_400_BAD_REQUEST, LINK_INVALIDO, "token_invalido")

    token_id, email_novo = registro.id, registro.email_novo
    if _email_em_uso(db, email_novo, usuario.id):
        registro.usado_em = func.now()
        db.commit()
        raise _recusar(http_status.HTTP_409_CONFLICT, EMAIL_EM_USO, "email_em_uso_na_confirmacao")

    email_antigo, nome = usuario.email, usuario.nome
    usuario.email = email_novo
    encerrar_sessoes(usuario)
    _cancelar_pendentes(db, usuario.id, None)  # inclusive este token
    try:
        db.commit()
    except IntegrityError:
        # Um cadastro com o e-mail novo entrou entre a conferência e o commit.
        db.rollback()
        db.execute(update(TokenEmail).where(TokenEmail.id == token_id).values(usado_em=func.now()))
        db.commit()
        raise _recusar(
            http_status.HTTP_409_CONFLICT, EMAIL_EM_USO, "email_em_uso_na_confirmacao"
        ) from None
    logger.info("troca_email resultado=confirmado")

    if provedor is None:
        logger.info("aviso_troca_email_concluida resultado=desabilitado")
    else:
        aviso = mensagem_troca_email_concluida(
            para=email_antigo,
            nome=nome,
            email_novo=email_novo,
            chave_idempotencia=f"troca-email-concluida/{token_id}",
        )
        tarefas.add_task(enviar_e_registrar, provedor, aviso, "aviso_troca_email_concluida")
    return ConfirmacaoTrocaEmailResponse(email=email_novo)
