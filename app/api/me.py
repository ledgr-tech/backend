"""Rotas da própria conta de quem está logado (issue #66, ADR-012).

`GET /me` devolve os dados da conta pro cabeçalho do front (issue #81): nome
da empresa, CNPJ e os métodos de login. Desde a issue #69, também
`administrador`: sem papéis (ADR-015), só quem cadastrou a empresa é
administrador, e é ele quem convida e remove usuários. Desde a issue #122,
também `plano`, `teste_termina_em` (em UTC) e `situacao` da empresa, para
todos os usuários dela, convidados inclusive: a mesma conta do
`POST /empresa/plano` (app/api/plano.py) e do bloqueio do `POST /conciliacoes`.

`POST /me/senha` troca a senha na hora, com a senha atual e a nova (mesma
regra do cadastro). As regras vêm da issue:

- Senha atual incorreta devolve 400 "Senha atual incorreta.", nunca 401: o
  frontend trata todo 401 como sessão expirada e deslogaria a pessoa por um
  erro de digitação. Não há risco de enumeração, o token já diz quem é.
- Conta sem senha (login pelo Google) devolve 409. Definir senha pra essa
  conta, ou vincular o Google a uma conta com senha, fica para depois do MVP
  (issue #73). Nenhuma troca acontece sem a senha atual: com um token
  roubado, isso viraria acesso permanente à conta.
- Rate limit por usuário (`sub`), não por IP (ver app/core/rate_limit.py).

A troca invalida os links de recuperação pendentes (um link antigo não
desfaz a troca) e manda o aviso de senha alterada por e-mail, depois da
resposta. Com o e-mail desligado, a troca vale do mesmo jeito, sem aviso.
A troca também encerra todas as sessões abertas, inclusive a de quem
trocou (issue #75): o token com login anterior deixa de valer. O backend
não devolve token novo: quem emite o JWT é o NextAuth, e o front
reautentica com a senha nova.

`DELETE /me` desativa a conta de quem está logado (issue #68, ADR-017). A
exclusão dos dados da empresa não tem rota: é processo interno, a pedido
(scripts/excluir_empresa.py).

- Conta com senha: `senha_atual` obrigatória e conferida. Errada ou ausente
  dá 400 "Senha atual incorreta.", nunca 401, pelo mesmo motivo da troca.
- Conta só do Google: não há senha para conferir, então vale o login
  recente, com `auth_time` de no máximo `LOGIN_RECENTE_SEGUNDOS` atrás
  (app/core/auth.py, mesmo relógio da #75). Senão, 409: o front pede o
  Google de novo e repete.
- Administrador com outro usuário ativo na empresa: 409. Sozinho, pode
  desativar; a empresa fica sem acesso, e os dados só saem pelo script.
- Rate limit por usuário, como a troca de senha.

A desativação é `remover_usuario` (app/services/usuarios.py): a linha fica
com o nome, para o histórico de decisões; o e-mail é liberado; senha e
`google_sub` são apagados; as sessões são encerradas. Os convites pendentes
que a pessoa mandou são cancelados. Com o e-mail ligado, o aviso vai para o
e-mail que a conta tinha, depois do commit. Nunca loga e-mail nem nome: só o
resultado categorizado.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.auth import SenhaNova
from app.api.plano import resposta_do_plano
from app.api.senha import agendar_aviso_senha_alterada, invalidar_links_de_recuperacao
from app.core import senha
from app.core.auth import (
    SessaoAutenticada,
    encerrar_sessoes,
    login_recente,
    obter_sessao_autenticada,
    obter_usuario_autenticado,
)
from app.core.database import get_db
from app.core.plano import Situacao
from app.core.rate_limit import (
    LIMITE_CONSULTA_ME_POR_USUARIO,
    LIMITE_DESATIVACAO_CONTA_POR_USUARIO,
    LIMITE_TROCA_SENHA_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import Convite, Usuario
from app.services.convites import condicoes_pendente
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_conta_desativada
from app.services.usuarios import remover_usuario

__all__ = [
    "LIMITE_CONSULTA_ME_POR_USUARIO",
    "LIMITE_DESATIVACAO_CONTA_POR_USUARIO",
    "LIMITE_TROCA_SENHA_POR_USUARIO",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["conta"])


class MeResponse(BaseModel):
    id: uuid.UUID
    empresa_id: uuid.UUID
    nome: str
    email: str
    razao_social: str
    cnpj: str
    administrador: bool
    metodos_login: list[str]
    plano: str | None
    teste_termina_em: datetime | None
    situacao: Situacao


def _metodos_login(usuario: Usuario) -> list[str]:
    metodos = []
    if usuario.senha_hash is not None:
        metodos.append("senha")
    if usuario.google_sub is not None:
        metodos.append("google")
    return metodos


@router.get("", response_model=MeResponse)
@limiter.limit(LIMITE_CONSULTA_ME_POR_USUARIO, key_func=chave_por_usuario)
def obter_dados_da_conta(
    request: Request,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
) -> MeResponse:
    empresa = usuario.empresa  # única consulta extra; a dependency já carregou o usuário
    return MeResponse(
        id=usuario.id,
        empresa_id=usuario.empresa_id,
        nome=usuario.nome,
        email=usuario.email,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        administrador=usuario.administrador,
        metodos_login=_metodos_login(usuario),
        **resposta_do_plano(empresa).model_dump(),
    )


class TrocaSenhaRequest(BaseModel):
    senha_atual: str = Field(min_length=1)
    senha_nova: SenhaNova


@router.post("/senha", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_TROCA_SENHA_POR_USUARIO, key_func=chave_por_usuario)
def trocar_senha(
    request: Request,
    dados: TrocaSenhaRequest,
    tarefas: BackgroundTasks,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> None:
    if usuario.senha_hash is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Esta conta entra pelo Google e não tem senha.",
        )
    if not senha.verificar(dados.senha_atual, usuario.senha_hash):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Senha atual incorreta.",
        )

    usuario.senha_hash = senha.gerar_hash(dados.senha_nova)
    encerrar_sessoes(usuario)
    invalidar_links_de_recuperacao(db, usuario.id)
    para, nome = usuario.email, usuario.nome  # antes do commit, que expira o objeto
    db.commit()
    agendar_aviso_senha_alterada(tarefas, provedor, para, nome)


class DesativacaoContaRequest(BaseModel):
    # Sem `min_length`: ausente ou vazia é senha incorreta (400), não 422.
    senha_atual: str | None = None


@router.delete("", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_DESATIVACAO_CONTA_POR_USUARIO, key_func=chave_por_usuario)
def desativar_conta(
    request: Request,
    tarefas: BackgroundTasks,
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
    dados: DesativacaoContaRequest | None = None,
) -> None:
    usuario = sessao.usuario
    # FOR UPDATE: duas desativações, ou a desativação e a remoção pelo
    # administrador, não passam as duas.
    db.refresh(usuario, with_for_update=True)
    if usuario.removido_em is not None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if usuario.senha_hash is not None:
        senha_atual = dados.senha_atual if dados is not None else None
        if not senha_atual or not senha.verificar(senha_atual, usuario.senha_hash):
            logger.info("desativacao_conta resultado=senha_incorreta")
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Senha atual incorreta.",
            )
    elif not login_recente(sessao.auth_time):
        logger.info("desativacao_conta resultado=login_antigo")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Entre de novo com o Google para confirmar a desativação.",
        )

    # Antes de contar os outros usuários: o UPDATE trava os convites
    # pendentes, então um aceite em andamento termina antes (e a contagem
    # abaixo, numa consulta nova, já vê o usuário criado) ou encontra o
    # convite cancelado.
    db.execute(
        update(Convite)
        .where(Convite.convidado_por_id == usuario.id, *condicoes_pendente())
        .values(cancelado_em=func.now())
    )
    if usuario.administrador and db.scalar(
        select(func.count()).where(
            Usuario.empresa_id == usuario.empresa_id,
            Usuario.removido_em.is_(None),
            Usuario.id != usuario.id,
        )
    ):
        db.rollback()
        logger.info("desativacao_conta resultado=administrador_com_usuarios")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Remova os outros usuários da empresa antes de desativar a sua conta.",
        )

    # Antes da remoção, que troca o e-mail, e do commit, que expira o objeto.
    mensagem = mensagem_conta_desativada(
        para=usuario.email,
        nome=usuario.nome,
        razao_social_sem_acesso=usuario.empresa.razao_social if usuario.administrador else None,
        chave_idempotencia=f"conta_desativada/{usuario.id}",
    )
    remover_usuario(db, usuario)
    db.commit()
    logger.info("desativacao_conta resultado=desativada")

    if provedor is None:
        logger.info("aviso_conta_desativada resultado=desabilitado")
        return
    tarefas.add_task(enviar_e_registrar, provedor, mensagem, "aviso_conta_desativada")
