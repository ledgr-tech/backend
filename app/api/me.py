"""Rotas da própria conta de quem está logado (issue #66, ADR-012).

`GET /me` devolve os dados da conta pro cabeçalho do front (issue #81): nome
da empresa, CNPJ e os métodos de login. Conta única no MVP, então sem campo
de papel — sem ele, o front trata todo mundo como administrador.

`POST /me/senha` troca a senha na hora, com a senha atual e a nova (mesma
regra do cadastro). As regras vêm da issue:

- Senha atual incorreta devolve 400 "Senha atual incorreta.", nunca 401: o
  frontend trata todo 401 como sessão expirada e deslogaria a pessoa por um
  erro de digitação. Não há risco de enumeração, o token já diz quem é.
- Conta sem senha (login pelo Google) devolve 409. Definir senha pra essa
  conta fica pra #73. Nenhuma troca acontece sem a senha atual: com um token
  roubado, isso viraria acesso permanente à conta.
- Rate limit por usuário (`sub`), não por IP (ver app/core/rate_limit.py).

A troca invalida os links de recuperação pendentes (um link antigo não
desfaz a troca) e manda o aviso de senha alterada por e-mail, depois da
resposta. Com o e-mail desligado, a troca vale do mesmo jeito, sem aviso.
O backend não devolve token novo: quem emite o JWT é o NextAuth, e o front
reautentica com a senha nova. Revogar as outras sessões depende da #75.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import SenhaNova
from app.api.senha import (
    agendar_aviso_senha_alterada,
    invalidar_links_de_recuperacao,
    obter_provedor_email_dependencia,
)
from app.core import senha
from app.core.auth import obter_usuario_autenticado
from app.core.database import get_db
from app.core.rate_limit import (
    LIMITE_CONSULTA_ME_POR_USUARIO,
    LIMITE_TROCA_SENHA_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import Usuario
from app.services.email import ProvedorDeEmail

__all__ = ["LIMITE_CONSULTA_ME_POR_USUARIO", "LIMITE_TROCA_SENHA_POR_USUARIO", "router"]

router = APIRouter(prefix="/me", tags=["conta"])


class MeResponse(BaseModel):
    id: uuid.UUID
    empresa_id: uuid.UUID
    nome: str
    email: str
    razao_social: str
    cnpj: str
    metodos_login: list[str]


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
        metodos_login=_metodos_login(usuario),
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
    invalidar_links_de_recuperacao(db, usuario.id)
    para, nome = usuario.email, usuario.nome  # antes do commit, que expira o objeto
    db.commit()
    agendar_aviso_senha_alterada(tarefas, provedor, para, nome)
