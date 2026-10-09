"""Recuperação de senha por e-mail (issue #66, ADR-012).

`POST /senha/recuperar` recebe o e-mail e devolve sempre a mesma resposta
(202), exista a conta ou não, pelo mesmo motivo do 401 único do `/login`:
não revelar quem tem conta. Isso vale também para o tempo de resposta. A
rota não consulta o banco: tudo o que depende de a conta existir (buscar
o usuário, gravar o token, chamar o Resend) roda em `BackgroundTasks`,
depois da resposta (o mesmo cuidado do `_HASH_FICTICIO` em
app/core/senha.py). Quem não recebe link, com a mesma resposta:

- e-mail sem conta;
- conta sem senha, que entra pelo Google (definir senha fica para a #73);
- destino que já recebeu `LIMITE_ENVIOS_POR_HORA` links na última hora, pra
  rota não virar ferramenta de envio em massa nem de assédio a um endereço.

A única resposta diferente é o 503, quando o envio está desligado ou sem
`FRONTEND_URL`. Ela depende só da configuração, igual pra qualquer e-mail.
Desde a issue #79, `FRONTEND_URL` recusada (URL de deploy da Vercel, http fora
de localhost) vale o mesmo que ausente — mesmo 503, mesmo corpo —, porque um
link montado em cima dela quebraria no deploy seguinte do front. Quem decide é
`app/core/frontend_url.py`, que loga o motivo.

`POST /senha/redefinir` recebe o token do link e a senha nova (mesma regra
do cadastro). Token inexistente, expirado, já usado ou de outra finalidade
devolve 400, nunca 401: o frontend trata todo 401 como sessão expirada. O
uso marca como usados todos os links pendentes do usuário e manda o aviso de
senha alterada (o mesmo da troca em `POST /me/senha`, app/api/me.py).

Tokens: `secrets.token_urlsafe(32)`, gravados só como sha256 (ver
app/models/token_email.py), com validade de `VALIDADE_TOKEN_MINUTOS`; um
pedido novo invalida o pendente anterior. O link vai no fragmento
(`/redefinir-senha#token=...`), que o navegador não manda ao servidor da
Vercel, então o token não aparece em log de acesso nem em `Referer`.

Revogar as sessões abertas depois da redefinição depende da #75, ainda
aberta: entra quando ela fechar.

Nunca loga o token, o link nem o e-mail: só o resultado categorizado.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.auth import SenhaNova, normalizar_email
from app.core import bloqueio_login, senha
from app.core.database import SessionLocal, get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import LIMITE_RECUPERACAO_SENHA, LIMITE_REDEFINICAO_SENHA, limiter
from app.models import TokenEmail, Usuario
from app.models.token_email import FINALIDADE_RECUPERACAO_SENHA
from app.services.email import ProvedorDeEmail, ProvedorEmailIndisponivel, obter_provedor_email
from app.services.email.base import MensagemEmail
from app.services.email.mensagens import mensagem_recuperacao_senha, mensagem_senha_alterada

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/senha", tags=["autenticacao"])

VALIDADE_TOKEN_MINUTOS = 30
LIMITE_ENVIOS_POR_HORA = 3

MENSAGEM_PEDIDO = "Se o e-mail estiver cadastrado, enviaremos um link para redefinir a senha."


class PedidoRecuperacaoRequest(BaseModel):
    email: EmailStr


class PedidoRecuperacaoResponse(BaseModel):
    mensagem: str


class RedefinicaoRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    senha_nova: SenhaNova


def obter_provedor_email_dependencia() -> ProvedorDeEmail | None:
    return obter_provedor_email()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invalidar_links_de_recuperacao(db: Session, usuario_id: uuid.UUID) -> None:
    db.execute(
        update(TokenEmail)
        .where(
            TokenEmail.usuario_id == usuario_id,
            TokenEmail.finalidade == FINALIDADE_RECUPERACAO_SENHA,
            TokenEmail.usado_em.is_(None),
        )
        .values(usado_em=func.now())
    )


def _enviar(provedor: ProvedorDeEmail, mensagem: MensagemEmail, evento: str) -> None:
    """Envia e só registra o resultado: roda depois da resposta, então uma
    falha não tem a quem ser devolvida."""
    try:
        provedor.enviar(mensagem)
    except ProvedorEmailIndisponivel as exc:
        logger.info(
            "%s resultado=falha_envio motivo=%s status_http=%s",
            evento,
            exc.motivo,
            exc.status_http,
        )
    except Exception as exc:  # noqa: BLE001 - a resposta já saiu, só registra
        logger.info("%s resultado=falha_envio classe=%s", evento, type(exc).__name__)
    else:
        logger.info("%s resultado=enviado", evento)


def enviar_aviso_senha_alterada(
    provedor: ProvedorDeEmail, para: str, nome: str, frontend_url: str
) -> None:
    """Aviso depois de qualquer troca de senha (redefinição pelo link ou
    `POST /me/senha`). Roda em `BackgroundTasks`, depois do commit."""
    base = frontend_url.strip().rstrip("/")
    mensagem = mensagem_senha_alterada(
        para=para, nome=nome, link_login=f"{base}/login" if base else None
    )
    _enviar(provedor, mensagem, "aviso_senha_alterada")


def agendar_aviso_senha_alterada(
    tarefas: BackgroundTasks, provedor: ProvedorDeEmail | None, para: str, nome: str
) -> None:
    """Com o e-mail desligado, a troca vale do mesmo jeito, só sem aviso."""
    if provedor is None:
        logger.info("aviso_senha_alterada resultado=desabilitado")
        return
    # URL recusada vira string vazia: o aviso continua indo, só sem o link de
    # login (ver `enviar_aviso_senha_alterada`). Trocar a senha não pode
    # depender de FRONTEND_URL estar certa.
    tarefas.add_task(
        enviar_aviso_senha_alterada, provedor, para, nome, frontend_url_para_link() or ""
    )


def enviar_link_recuperacao(email: str, provedor: ProvedorDeEmail, frontend_url: str) -> None:
    """Roda em `BackgroundTasks`, com sessão própria: a do request já foi
    fechada quando a tarefa roda. A transação termina antes da chamada ao
    Resend, que nunca acontece com banco aberto (mesma regra do ADR-011)."""
    with SessionLocal() as db:
        # FOR UPDATE na linha do usuário serializa pedidos simultâneos do
        # mesmo e-mail: sem isso, os dois passariam pela contagem e
        # deixariam dois links válidos.
        usuario = db.scalar(select(Usuario).where(Usuario.email == email).with_for_update())
        if usuario is None or usuario.senha_hash is None:
            logger.info("recuperacao_senha resultado=sem_envio")
            return

        envios_na_ultima_hora = db.scalar(
            select(func.count())
            .select_from(TokenEmail)
            .where(
                TokenEmail.usuario_id == usuario.id,
                TokenEmail.finalidade == FINALIDADE_RECUPERACAO_SENHA,
                TokenEmail.criado_em >= func.now() - timedelta(hours=1),
            )
        )
        if envios_na_ultima_hora >= LIMITE_ENVIOS_POR_HORA:
            logger.info("recuperacao_senha resultado=limite_por_destino")
            return

        invalidar_links_de_recuperacao(db, usuario.id)
        token = secrets.token_urlsafe(32)
        token_id = uuid.uuid4()
        db.add(
            TokenEmail(
                id=token_id,
                usuario_id=usuario.id,
                finalidade=FINALIDADE_RECUPERACAO_SENHA,
                token_hash=_hash_token(token),
                expira_em=func.now() + timedelta(minutes=VALIDADE_TOKEN_MINUTOS),
            )
        )
        mensagem = mensagem_recuperacao_senha(
            para=usuario.email,
            nome=usuario.nome,
            link=f"{frontend_url.rstrip('/')}/redefinir-senha#token={token}",
            validade_minutos=VALIDADE_TOKEN_MINUTOS,
            chave_idempotencia=f"recuperacao-senha/{token_id}",
        )
        db.commit()

    _enviar(provedor, mensagem, "recuperacao_senha")


@router.post(
    "/recuperar",
    response_model=PedidoRecuperacaoResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
)
@limiter.limit(LIMITE_RECUPERACAO_SENHA)
def pedir_recuperacao(
    request: Request,
    dados: PedidoRecuperacaoRequest,
    tarefas: BackgroundTasks,
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> PedidoRecuperacaoResponse:
    frontend_url = frontend_url_para_link()
    if provedor is None or not frontend_url:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recuperação de senha indisponível no momento.",
        )

    tarefas.add_task(enviar_link_recuperacao, normalizar_email(dados.email), provedor, frontend_url)
    return PedidoRecuperacaoResponse(mensagem=MENSAGEM_PEDIDO)


@router.post("/redefinir", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_REDEFINICAO_SENHA)
def redefinir_senha(
    request: Request,
    dados: RedefinicaoRequest,
    tarefas: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> None:
    # FOR UPDATE: dois usos simultâneos do mesmo link não passam os dois. O
    # segundo espera, e o Postgres reavalia `usado_em IS NULL` na linha já
    # atualizada.
    registro = db.scalar(
        select(TokenEmail)
        .where(
            TokenEmail.token_hash == _hash_token(dados.token),
            TokenEmail.finalidade == FINALIDADE_RECUPERACAO_SENHA,
            TokenEmail.usado_em.is_(None),
            TokenEmail.expira_em > func.now(),
        )
        .with_for_update()
    )
    usuario = db.get(Usuario, registro.usuario_id) if registro else None
    if usuario is None or usuario.senha_hash is None:
        logger.info("redefinicao_senha resultado=token_invalido")
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Link inválido ou expirado. Peça um novo.",
        )

    usuario.senha_hash = senha.gerar_hash(dados.senha_nova)
    invalidar_links_de_recuperacao(db, usuario.id)
    para, nome = usuario.email, usuario.nome  # antes do commit, que expira o objeto
    db.commit()
    # O link de recuperação destrava o login bloqueado por senha errada (issue #67).
    bloqueio_login.limpar(para)
    logger.info("redefinicao_senha resultado=sucesso")
    agendar_aviso_senha_alterada(tarefas, provedor, para, nome)
