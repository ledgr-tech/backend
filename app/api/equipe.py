"""Equipe da empresa: convites e usuários (issue #69, ADR-015).

Sem papéis, só o administrador: quem cadastrou a empresa pelo `/register`.
Ele convida, cancela convite e remove usuário (403 para os outros, pela
dependency `obter_administrador_autenticado`). Qualquer usuário da empresa vê
a lista de usuários. Tudo sempre pela empresa do token: id de convite ou de
usuário de outra empresa é 404, com a mesma mensagem do inexistente.

`POST /empresa/convites`:

- 503, sem gravar nada, quando o e-mail está indisponível (desligado, sem
  chave, sem remetente ou `FRONTEND_URL` recusada), a mesma regra do
  `/senha/recuperar` (ADR-012);
- 429 com `Retry-After` depois de `LIMITE_CONVITES_POR_HORA` convites da
  empresa na última hora, contados na tabela. A linha da empresa é lida com
  `FOR UPDATE`, então dois convites simultâneos não passam os dois pela
  contagem;
- 409 quando o e-mail já tem usuário ativo no Ledgr, em qualquer empresa. Não
  revela nada novo: o `/register/verificar` já informa;
- um convite novo para o mesmo e-mail na mesma empresa cancela o pendente.

O token (`secrets.token_urlsafe(32)`) é gravado só como sha256, vale
`VALIDADE_CONVITE_DIAS` e uma vez só. O link vai no fragmento
(`/aceitar-convite#token=...`), como na recuperação de senha, e o e-mail sai
em `BackgroundTasks`, depois do commit.

`DELETE /empresa/usuarios/{id}` aplica `remover_usuario`
(app/services/usuarios.py). O administrador não pode ser removido, nem por
ele mesmo (409).

Nunca loga e-mail, nome, token nem link: só o resultado categorizado.
"""

import logging
import math
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, EmailStr
from sqlalchemy import DateTime, cast, func, select, update
from sqlalchemy.orm import Session

from app.core.auth import (
    normalizar_email,
    obter_administrador_autenticado,
    obter_usuario_autenticado,
)
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.models import Convite, Empresa, Usuario
from app.services.convites import VALIDADE_CONVITE_DIAS, condicoes_pendente, hash_token
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_convite
from app.services.usuarios import remover_usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresa", tags=["equipe"])

LIMITE_CONVITES_POR_HORA = 10


class ConviteRequest(BaseModel):
    email: EmailStr


class ConviteResponse(BaseModel):
    id: uuid.UUID
    email: str
    criado_em: datetime
    expira_em: datetime


class ConviteListaResponse(BaseModel):
    itens: list[ConviteResponse]


class UsuarioEquipeResponse(BaseModel):
    id: uuid.UUID
    nome: str
    email: str
    administrador: bool
    criado_em: datetime


class UsuarioListaResponse(BaseModel):
    itens: list[UsuarioEquipeResponse]


def _utc(momento: datetime) -> datetime:
    """`criado_em` é `TIMESTAMP` sem fuso, gravado em UTC (TimestampMixin)."""
    return momento.replace(tzinfo=UTC) if momento.tzinfo is None else momento


def _convite_response(convite: Convite) -> ConviteResponse:
    return ConviteResponse(
        id=convite.id,
        email=convite.email,
        criado_em=_utc(convite.criado_em),
        expira_em=convite.expira_em,
    )


def _nao_encontrado(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=detalhe)


@router.post("/convites", response_model=ConviteResponse, status_code=http_status.HTTP_201_CREATED)
def convidar(
    dados: ConviteRequest,
    tarefas: BackgroundTasks,
    administrador: Annotated[Usuario, Depends(obter_administrador_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> ConviteResponse:
    frontend_url = frontend_url_para_link()
    if provedor is None or not frontend_url:
        logger.info("convite resultado=indisponivel")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Convite indisponível no momento.",
        )

    empresa_id = administrador.empresa_id
    empresa = db.scalar(select(Empresa).where(Empresa.id == empresa_id).with_for_update())

    # Tudo no relógio do Postgres, que é quem grava `criado_em`. O Retry-After
    # é o tempo até o convite mais antigo da janela sair dela.
    enviados, segundos_ate_liberar = db.execute(
        select(
            func.count(),
            func.extract(
                "epoch",
                func.min(cast(Convite.criado_em, DateTime(timezone=True)))
                + timedelta(hours=1)
                - func.now(),
            ),
        ).where(
            Convite.empresa_id == empresa_id,
            Convite.criado_em >= func.now() - timedelta(hours=1),
        )
    ).one()
    if enviados >= LIMITE_CONVITES_POR_HORA:
        logger.info("convite resultado=limite_por_hora")
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de convites por hora atingido. Tente novamente mais tarde.",
            headers={"Retry-After": str(max(1, math.ceil(segundos_ate_liberar)))},
        )

    email = normalizar_email(dados.email)
    if db.scalar(select(Usuario.id).where(Usuario.email == email, Usuario.removido_em.is_(None))):
        logger.info("convite resultado=email_em_uso")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="E-mail já cadastrado."
        )

    db.execute(
        update(Convite)
        .where(Convite.empresa_id == empresa_id, Convite.email == email, *condicoes_pendente())
        .values(cancelado_em=func.now())
    )
    token = secrets.token_urlsafe(32)
    convite = Convite(
        id=uuid.uuid4(),
        empresa_id=empresa_id,
        email=email,
        token_hash=hash_token(token),
        convidado_por_id=administrador.id,
        expira_em=func.now() + timedelta(days=VALIDADE_CONVITE_DIAS),
    )
    db.add(convite)
    mensagem = mensagem_convite(
        para=email,
        nome_de_quem_convidou=administrador.nome,
        razao_social=empresa.razao_social,
        link=f"{frontend_url}/aceitar-convite#token={token}",
        validade_dias=VALIDADE_CONVITE_DIAS,
        chave_idempotencia=f"convite/{convite.id}",
    )
    db.commit()
    db.refresh(convite)

    logger.info("convite resultado=criado")
    tarefas.add_task(enviar_e_registrar, provedor, mensagem, "convite")
    return _convite_response(convite)


@router.get("/convites", response_model=ConviteListaResponse)
def listar_convites(
    administrador: Annotated[Usuario, Depends(obter_administrador_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> ConviteListaResponse:
    convites = db.scalars(
        select(Convite)
        .where(Convite.empresa_id == administrador.empresa_id, *condicoes_pendente())
        .order_by(Convite.criado_em.desc(), Convite.id)
    )
    return ConviteListaResponse(itens=[_convite_response(convite) for convite in convites])


@router.delete("/convites/{convite_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def cancelar_convite(
    convite_id: uuid.UUID,
    administrador: Annotated[Usuario, Depends(obter_administrador_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Só convite pendente: usado, cancelado ou expirado já não aparece na
    lista, e aqui também é 404."""
    cancelado = db.execute(
        update(Convite)
        .where(
            Convite.id == convite_id,
            Convite.empresa_id == administrador.empresa_id,
            *condicoes_pendente(),
        )
        .values(cancelado_em=func.now())
    )
    if cancelado.rowcount == 0:
        raise _nao_encontrado("Convite não encontrado.")
    db.commit()
    logger.info("convite_cancelamento resultado=cancelado")


@router.get("/usuarios", response_model=UsuarioListaResponse)
def listar_usuarios(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioListaResponse:
    usuarios = db.scalars(
        select(Usuario)
        .where(Usuario.empresa_id == usuario.empresa_id, Usuario.removido_em.is_(None))
        .order_by(Usuario.criado_em, Usuario.id)
    )
    return UsuarioListaResponse(
        itens=[
            UsuarioEquipeResponse(
                id=u.id,
                nome=u.nome,
                email=u.email,
                administrador=u.administrador,
                criado_em=_utc(u.criado_em),
            )
            for u in usuarios
        ]
    )


@router.delete("/usuarios/{usuario_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def remover(
    usuario_id: uuid.UUID,
    administrador: Annotated[Usuario, Depends(obter_administrador_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    alvo = db.scalar(
        select(Usuario)
        .where(
            Usuario.id == usuario_id,
            Usuario.empresa_id == administrador.empresa_id,
            Usuario.removido_em.is_(None),
        )
        .with_for_update()
    )
    if alvo is None:
        raise _nao_encontrado("Usuário não encontrado.")
    if alvo.administrador or alvo.id == administrador.id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="O administrador da empresa não pode ser removido.",
        )

    remover_usuario(db, alvo)
    db.commit()
    logger.info("remocao_usuario resultado=removido")
