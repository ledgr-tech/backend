"""Equipe da conta: convites, usuários e papéis (issue #69, ADR-015; issue #133,
ADR-019).

Desde a issue #133, convites e usuários são da CONTA, e não de uma empresa
cliente: os caminhos continuam em `/empresa/*`, mas as rotas ignoram o
cabeçalho `X-Ledgr-Empresa`. Tudo sempre pela conta do token: id de convite ou
de usuário de outra conta é 404, com a mesma mensagem do inexistente. Os 403
saem de `exigir_papel` (app/core/auth.py) ou, onde depende do convite, daqui.

Papéis (matriz no ADR-019): o admin convida gestor ou analista, cancela
qualquer convite, muda o papel de quem não é admin, remove usuário e transfere
o papel de admin. O gestor convida só analista, vê os convites e cancela só
convite de analista. O analista não convida. Qualquer usuário da conta vê a
lista de usuários, com o `papel` de cada um (e `administrador`, obsoleto,
igual a `papel == "admin"`).

`POST /empresa/convites`, nesta ordem:

- 403 para o analista e para o gestor que convida gestor; `papel: "admin"` é
  422 (o admin só muda por transferência);
- 503, sem gravar nada, quando o e-mail está indisponível (desligado, sem
  chave, sem remetente ou `FRONTEND_URL` recusada), a mesma regra do
  `/senha/recuperar` (ADR-012);
- 429 com `Retry-After` depois de `LIMITE_CONVITES_POR_HORA` convites da
  conta na última hora, contados na tabela. A linha da conta é lida com
  `FOR UPDATE`, então dois convites simultâneos não passam os dois pela
  contagem, nem pelo limite do plano;
- 409 quando o e-mail já tem usuário ativo no Ledgr, em qualquer conta. Não
  revela nada novo: o `/register/verificar` já informa;
- 402 no limite de usuários do plano, para todos os papéis: usuários ativos
  mais convites pendentes (app/services/plano.py). O convite pendente para o
  mesmo e-mail não conta, porque o novo o substitui;
- um convite novo para o mesmo e-mail na mesma conta cancela o pendente.

O token (`secrets.token_urlsafe(32)`) é gravado só como sha256, vale
`VALIDADE_CONVITE_DIAS` e uma vez só. O link vai no fragmento
(`/aceitar-convite#token=...`), como na recuperação de senha, e o e-mail sai
em `BackgroundTasks`, depois do commit.

`PATCH /empresa/usuarios/{id}` muda o papel entre gestor e analista. O admin
só muda de papel pela transferência: mudar o próprio papel ou o do admin é
409. O mesmo papel de novo é 200 sem mudar nada.

`POST /empresa/admin/transferencia` passa o papel de admin para outro usuário
ativo da conta, com a confirmação do `DELETE /me` (issue #68): senha atual na
conta com senha (400 "Senha atual incorreta.", nunca 401), login recente na
conta só do Google (409 "Entre de novo com o Google para confirmar."). Como
confere a senha, tem rate limit por usuário, como a troca de senha. As duas linhas são lidas com `FOR UPDATE`, em ordem
de id, e numa transação só o admin vira gestor e o destino vira admin: o
rebaixamento vai ao banco antes da promoção, por causa do índice único
parcial de um admin por conta. Duas transferências simultâneas não passam as
duas: a segunda encontra quem pediu já como gestor e recebe 409. Nenhuma
sessão é encerrada, porque o papel é lido do banco a cada requisição.

`DELETE /empresa/usuarios/{id}` aplica `remover_usuario`
(app/services/usuarios.py). O admin não pode ser removido, nem por ele mesmo
(409): a conta tem sempre um admin.

Nunca loga e-mail, nome, token nem link: só o resultado categorizado.
"""

import logging
import math
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import DateTime, cast, func, select, update
from sqlalchemy.orm import Session

from app.api.me import SENHA_INCORRETA
from app.core import senha
from app.core.auth import (
    SessaoAutenticada,
    exigir_papel,
    login_recente,
    normalizar_email,
    obter_admin_autenticado,
    obter_sessao_autenticada,
    obter_usuario_autenticado,
)
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import (
    LIMITE_TRANSFERENCIA_ADMIN_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import Conta, Convite, Usuario
from app.models.usuario import Papel, PapelSemAdmin
from app.services.convites import VALIDADE_CONVITE_DIAS, condicoes_pendente, hash_token
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_convite
from app.services.plano import barrar_no_limite, usuarios_usados
from app.services.usuarios import remover_usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresa", tags=["equipe"])

LIMITE_CONVITES_POR_HORA = 10

# A conta só do Google confirma a transferência entrando de novo.
CONFIRMAR_TRANSFERENCIA_COM_O_GOOGLE = "Entre de novo com o Google para confirmar."

obter_admin_ou_gestor = exigir_papel("admin", "gestor")


class ConviteRequest(BaseModel):
    email: EmailStr
    papel: PapelSemAdmin = "analista"


class ConviteResponse(BaseModel):
    id: uuid.UUID
    email: str
    papel: PapelSemAdmin
    criado_em: datetime
    expira_em: datetime


class ConviteListaResponse(BaseModel):
    itens: list[ConviteResponse]


class UsuarioEquipeResponse(BaseModel):
    id: uuid.UUID
    nome: str
    email: str
    papel: Papel
    administrador: bool = Field(
        description='Obsoleto: igual a `papel == "admin"` (issue #133). Use `papel`.',
        json_schema_extra={"deprecated": True},
    )
    criado_em: datetime


class UsuarioListaResponse(BaseModel):
    itens: list[UsuarioEquipeResponse]


class TrocaPapelRequest(BaseModel):
    papel: PapelSemAdmin


class TransferenciaAdminRequest(BaseModel):
    usuario_id: uuid.UUID
    # Sem `min_length`: ausente ou vazia é senha incorreta (400), não 422.
    senha_atual: str | None = None


def _utc(momento: datetime) -> datetime:
    """`criado_em` é `TIMESTAMP` sem fuso, gravado em UTC (TimestampMixin)."""
    return momento.replace(tzinfo=UTC) if momento.tzinfo is None else momento


def _convite_response(convite: Convite) -> ConviteResponse:
    return ConviteResponse(
        id=convite.id,
        email=convite.email,
        papel=convite.papel,
        criado_em=_utc(convite.criado_em),
        expira_em=convite.expira_em,
    )


def _usuario_response(usuario: Usuario) -> UsuarioEquipeResponse:
    return UsuarioEquipeResponse(
        id=usuario.id,
        nome=usuario.nome,
        email=usuario.email,
        papel=usuario.papel,
        administrador=usuario.papel == "admin",
        criado_em=_utc(usuario.criado_em),
    )


def _nao_encontrado(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=detalhe)


def _proibido(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=detalhe)


def _conflito(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detalhe)


USUARIO_NAO_ENCONTRADO = "Usuário não encontrado."


@router.post("/convites", response_model=ConviteResponse, status_code=http_status.HTTP_201_CREATED)
def convidar(
    dados: ConviteRequest,
    tarefas: BackgroundTasks,
    quem_convida: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> ConviteResponse:
    if dados.papel == "gestor" and quem_convida.papel != "admin":
        logger.info("convite resultado=gestor_convidando_gestor")
        raise _proibido("Só o admin da conta pode convidar gestores.")

    frontend_url = frontend_url_para_link()
    if provedor is None or not frontend_url:
        logger.info("convite resultado=indisponivel")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Convite indisponível no momento.",
        )

    conta_id = quem_convida.conta_id
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

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
            Convite.conta_id == conta_id,
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
        raise _conflito("E-mail já cadastrado.")

    try:
        barrar_no_limite(conta, "usuarios", usuarios_usados(db, conta_id, sem_o_convite_para=email))
    except HTTPException:
        logger.info("convite resultado=limite_do_plano")
        raise

    db.execute(
        update(Convite)
        .where(Convite.conta_id == conta_id, Convite.email == email, *condicoes_pendente())
        .values(cancelado_em=func.now())
    )
    token = secrets.token_urlsafe(32)
    convite = Convite(
        id=uuid.uuid4(),
        conta_id=conta_id,
        email=email,
        papel=dados.papel,
        token_hash=hash_token(token),
        convidado_por_id=quem_convida.id,
        expira_em=func.now() + timedelta(days=VALIDADE_CONVITE_DIAS),
    )
    db.add(convite)
    mensagem = mensagem_convite(
        para=email,
        nome_de_quem_convidou=quem_convida.nome,
        razao_social=conta.razao_social,
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
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> ConviteListaResponse:
    convites = db.scalars(
        select(Convite)
        .where(Convite.conta_id == usuario.conta_id, *condicoes_pendente())
        .order_by(Convite.criado_em.desc(), Convite.id)
    )
    return ConviteListaResponse(itens=[_convite_response(convite) for convite in convites])


@router.delete("/convites/{convite_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def cancelar_convite(
    convite_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Só convite pendente: usado, cancelado ou expirado já não aparece na
    lista, e aqui também é 404. O gestor cancela só convite de analista."""
    convite = db.scalar(
        select(Convite)
        .where(
            Convite.id == convite_id,
            Convite.conta_id == usuario.conta_id,
            *condicoes_pendente(),
        )
        .with_for_update()
    )
    if convite is None:
        raise _nao_encontrado("Convite não encontrado.")
    if usuario.papel != "admin" and convite.papel != "analista":
        logger.info("convite_cancelamento resultado=gestor_cancelando_gestor")
        raise _proibido("Só o admin da conta pode cancelar convites de gestores.")
    convite.cancelado_em = func.now()
    db.commit()
    logger.info("convite_cancelamento resultado=cancelado")


def _usuarios_da_conta(db: Session, conta_id: uuid.UUID) -> UsuarioListaResponse:
    usuarios = db.scalars(
        select(Usuario)
        .where(Usuario.conta_id == conta_id, Usuario.removido_em.is_(None))
        .order_by(Usuario.criado_em, Usuario.id)
    )
    return UsuarioListaResponse(itens=[_usuario_response(u) for u in usuarios])


@router.get("/usuarios", response_model=UsuarioListaResponse)
def listar_usuarios(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioListaResponse:
    return _usuarios_da_conta(db, usuario.conta_id)


def _usuario_ativo_da_conta(
    db: Session, usuario_id: uuid.UUID, conta_id: uuid.UUID
) -> Usuario | None:
    """O usuário ativo da conta, com a linha travada e relida do banco."""
    return db.scalar(
        select(Usuario)
        .where(
            Usuario.id == usuario_id,
            Usuario.conta_id == conta_id,
            Usuario.removido_em.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


SO_PELA_TRANSFERENCIA = "O admin só muda de papel pela transferência."


@router.patch("/usuarios/{usuario_id}", response_model=UsuarioEquipeResponse)
def trocar_papel(
    usuario_id: uuid.UUID,
    dados: TrocaPapelRequest,
    admin: Annotated[Usuario, Depends(obter_admin_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioEquipeResponse:
    alvo = _usuario_ativo_da_conta(db, usuario_id, admin.conta_id)
    if alvo is None:
        raise _nao_encontrado(USUARIO_NAO_ENCONTRADO)
    if alvo.id == admin.id or alvo.papel == "admin":
        logger.info("troca_papel resultado=admin")
        raise _conflito(SO_PELA_TRANSFERENCIA)
    if alvo.papel == dados.papel:
        db.rollback()
        logger.info("troca_papel resultado=mesmo_papel")
        return _usuario_response(alvo)

    alvo.papel = dados.papel
    db.commit()
    db.refresh(alvo)
    logger.info("troca_papel resultado=trocado")
    return _usuario_response(alvo)


@router.post("/admin/transferencia", response_model=UsuarioListaResponse)
@limiter.limit(LIMITE_TRANSFERENCIA_ADMIN_POR_USUARIO, key_func=chave_por_usuario)
def transferir_admin(
    request: Request,
    dados: TransferenciaAdminRequest,
    admin: Annotated[Usuario, Depends(obter_admin_autenticado)],
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
    db: Annotated[Session, Depends(get_db)],
) -> UsuarioListaResponse:
    if admin.senha_hash is not None:
        if not dados.senha_atual or not senha.verificar(dados.senha_atual, admin.senha_hash):
            logger.info("transferencia_admin resultado=senha_incorreta")
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST, detail=SENHA_INCORRETA
            )
    elif not login_recente(sessao.auth_time):
        logger.info("transferencia_admin resultado=login_antigo")
        raise _conflito(CONFIRMAR_TRANSFERENCIA_COM_O_GOOGLE)

    if dados.usuario_id == admin.id:
        logger.info("transferencia_admin resultado=para_si_mesmo")
        raise _conflito("O papel de admin já é seu. Escolha outro usuário da conta.")

    # As duas linhas, travadas em ordem de id: duas transferências (ou uma
    # transferência e uma troca de papel) nunca se travam uma à outra.
    linhas = {
        u.id: u
        for u in db.scalars(
            select(Usuario)
            .where(
                Usuario.id.in_([admin.id, dados.usuario_id]),
                Usuario.conta_id == admin.conta_id,
            )
            .order_by(Usuario.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    atual, destino = linhas.get(admin.id), linhas.get(dados.usuario_id)
    if atual is None or atual.papel != "admin" or atual.removido_em is not None:
        # Outra transferência terminou antes desta.
        db.rollback()
        logger.info("transferencia_admin resultado=nao_e_mais_admin")
        raise _conflito("O papel de admin já foi transferido.")
    if destino is None or destino.removido_em is not None:
        db.rollback()
        logger.info("transferencia_admin resultado=destino_nao_encontrado")
        raise _nao_encontrado(USUARIO_NAO_ENCONTRADO)

    atual.papel = "gestor"
    db.flush()  # antes da promoção: o índice único parcial aceita um admin só
    destino.papel = "admin"
    db.commit()
    logger.info("transferencia_admin resultado=transferido")
    return _usuarios_da_conta(db, admin.conta_id)


@router.delete("/usuarios/{usuario_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def remover(
    usuario_id: uuid.UUID,
    admin: Annotated[Usuario, Depends(obter_admin_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    alvo = _usuario_ativo_da_conta(db, usuario_id, admin.conta_id)
    if alvo is None:
        raise _nao_encontrado(USUARIO_NAO_ENCONTRADO)
    if alvo.papel == "admin" or alvo.id == admin.id:
        raise _conflito("O admin da conta não pode ser removido.")

    remover_usuario(db, alvo)
    db.commit()
    logger.info("remocao_usuario resultado=removido")
