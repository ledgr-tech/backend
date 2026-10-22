"""Consentimento e conexões MCP, pelo app (issue #136, ADR-022; front #195).

Rotas da conta, com o JWT do app: ignoram `X-Ledgr-Empresa`.

- `GET /conexoes-mcp/pedidos/{pedido}`: o que a tela de consentimento mostra.
  O nome do cliente, o host do redirect_uri (de onde vem o pedido, como a
  especificação do MCP exige), se ele é loopback (qualquer programa da
  máquina pode se passar por cliente: a tela avisa), o escopo e as empresas
  da conta. Pedido inexistente, expirado ou já usado: 404. Qualquer papel lê.
- `POST /conexoes-mcp/pedidos/{pedido}/aprovar` (`empresa_id`): só admin e
  gestor. Cria a conexão (uma empresa, só leitura) e o código de
  autorização, e devolve `redirecionar_para`, o redirect_uri do cliente com
  `code`, `state` e `iss`. O front redireciona o navegador. No máximo
  `MAXIMO_CONEXOES_ATIVAS` ativas por conta (409), contadas com a linha da
  conta travada.
- `POST /conexoes-mcp/pedidos/{pedido}/recusar`: qualquer papel. Devolve o
  redirect_uri com `error=access_denied`, `state` e `iss`.
- `GET /conexoes-mcp`: admin e gestor; as conexões da conta, ativas e
  revogadas, das mais novas para as mais antigas.
- `DELETE /conexoes-mcp/{id}`: admin e gestor; revoga na hora (todos os
  tokens dela). 204, inclusive se já revogada; de outra conta, 404.

A conexão também morre quando quem a criou é removido, desativa a conta ou é
rebaixado a analista (app/services/conexoes_mcp.py). A regra de plano fica
em `app.core.plano.mcp_liberado`, que no MVP libera todos.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.api.oauth import exigir_mcp
from app.core.auth import exigir_papel, obter_usuario_autenticado
from app.core.database import get_db
from app.core.plano import mcp_liberado
from app.core.rate_limit import LIMITE_CONSENTIMENTO_MCP_POR_USUARIO, chave_por_usuario, limiter
from app.models import ClienteOAuth, ConexaoMcp, Conta, Empresa, PedidoAutorizacaoOAuth, Usuario
from app.services.conexoes_mcp import MAXIMO_CONEXOES_ATIVAS, revogar_conexao
from app.services.oauth.redirects import eh_loopback
from app.services.oauth.servidor import (
    ESCOPO_LEITURA,
    OAuth2Error,
    RequisicaoOAuth,
    ServidorOAuth,
    hash_do_segredo,
    redirecionar_com_erro,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/conexoes-mcp", tags=["conexoes-mcp"], dependencies=[Depends(exigir_mcp)]
)

obter_admin_ou_gestor = exigir_papel("admin", "gestor")

PEDIDO_NAO_ENCONTRADO = "Pedido de conexão não encontrado ou expirado."
DESCRICAO_ESCOPO = (
    "Ler o status das conciliações, as divergências por mês e os fechamentos de uma "
    "empresa. Sem descrição de lançamento, justificativas nem nomes de pessoas."
)


class EmpresaDoPedido(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str


class OrigemDoPedido(BaseModel):
    host: str
    loopback: bool


class PedidoResponse(BaseModel):
    cliente: str
    origem: OrigemDoPedido
    escopo: str
    descricao_escopo: str
    expira_em: datetime
    empresas: list[EmpresaDoPedido]


class AprovacaoRequest(BaseModel):
    empresa_id: uuid.UUID


class RedirecionamentoResponse(BaseModel):
    redirecionar_para: str


class EmpresaDaConexao(BaseModel):
    id: uuid.UUID
    razao_social: str


class ConexaoResponse(BaseModel):
    id: uuid.UUID
    cliente: str
    empresa: EmpresaDaConexao
    criada_por: str | None
    criado_em: datetime
    ultimo_uso_em: datetime | None
    revogada_em: datetime | None


class ConexaoListaResponse(BaseModel):
    itens: list[ConexaoResponse]


def _pedido_pendente(db: Session, pedido: str) -> PedidoAutorizacaoOAuth:
    """O pedido ainda válido, com a linha travada: aprovar e recusar ao mesmo
    tempo não passam os dois."""
    encontrado = db.scalar(
        select(PedidoAutorizacaoOAuth)
        .where(
            PedidoAutorizacaoOAuth.hash == hash_do_segredo(pedido),
            PedidoAutorizacaoOAuth.usado_em.is_(None),
            PedidoAutorizacaoOAuth.expira_em > func.now(),
        )
        .with_for_update()
    )
    if encontrado is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=PEDIDO_NAO_ENCONTRADO
        )
    return encontrado


def _requisicao_do_pedido(pedido: PedidoAutorizacaoOAuth) -> RequisicaoOAuth:
    """O authorize original, refeito a partir do pedido gravado, para o
    Authlib validar de novo e emitir o código."""
    dados = {
        "client_id": [pedido.client_id],
        "redirect_uri": [pedido.redirect_uri],
        "response_type": ["code"],
        "scope": [pedido.scope],
        "code_challenge": [pedido.code_challenge],
        "code_challenge_method": ["S256"],
        "resource": [pedido.resource],
    }
    if pedido.state is not None:
        dados["state"] = [pedido.state]
    return RequisicaoOAuth("GET", "/oauth/authorize", dados)


@router.get("/pedidos/{pedido}", response_model=PedidoResponse)
def ver_pedido(
    pedido: str,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> PedidoResponse:
    encontrado = _pedido_pendente(db, pedido)
    cliente = db.get(ClienteOAuth, encontrado.client_id)
    empresas = db.scalars(
        select(Empresa)
        .where(Empresa.conta_id == usuario.conta_id)
        .order_by(Empresa.razao_social, Empresa.id)
    )
    resposta = PedidoResponse(
        cliente=cliente.client_name or "Cliente sem nome",
        origem=OrigemDoPedido(
            host=urlsplit(encontrado.redirect_uri).hostname or "",
            loopback=eh_loopback(encontrado.redirect_uri),
        ),
        escopo=ESCOPO_LEITURA,
        descricao_escopo=DESCRICAO_ESCOPO,
        expira_em=encontrado.expira_em,
        empresas=[
            EmpresaDoPedido(id=e.id, razao_social=e.razao_social, cnpj=e.cnpj) for e in empresas
        ],
    )
    db.rollback()  # solta a trava: só leitura
    return resposta


@router.post("/pedidos/{pedido}/aprovar", response_model=RedirecionamentoResponse)
@limiter.limit(LIMITE_CONSENTIMENTO_MCP_POR_USUARIO, key_func=chave_por_usuario)
def aprovar_pedido(
    request: Request,
    pedido: str,
    dados: AprovacaoRequest,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> RedirecionamentoResponse:
    conta_id, usuario_id = usuario.conta_id, usuario.id
    encontrado = _pedido_pendente(db, pedido)
    empresa = db.get(Empresa, dados.empresa_id)
    if empresa is None or empresa.conta_id != conta_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    conta = db.scalar(select(Conta).where(Conta.id == conta_id).with_for_update())
    if not mcp_liberado(conta):  # sempre liberado no MVP
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail="O plano da sua conta não inclui a conexão com assistentes de IA.",
        )
    ativas = db.scalar(
        select(func.count()).where(
            ConexaoMcp.conta_id == conta_id, ConexaoMcp.revogada_em.is_(None)
        )
    )
    if ativas >= MAXIMO_CONEXOES_ATIVAS:
        logger.info("conexao_mcp resultado=limite_de_ativas")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"Sua conta já tem {MAXIMO_CONEXOES_ATIVAS} conexões ativas. "
                "Revogue uma antes de criar outra."
            ),
        )

    servidor = ServidorOAuth(db)
    requisicao = _requisicao_do_pedido(encontrado)
    try:
        grant = servidor.get_consent_grant(requisicao)
    except OAuth2Error:
        # O cliente sumiu ou mudou desde o authorize.
        db.rollback()
        logger.info("conexao_mcp resultado=pedido_invalido")
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=PEDIDO_NAO_ENCONTRADO
        ) from None

    cliente = db.get(ClienteOAuth, encontrado.client_id)
    conexao = ConexaoMcp(
        id=uuid.uuid4(),
        conta_id=conta_id,
        empresa_id=empresa.id,
        client_id=cliente.client_id,
        nome_cliente=cliente.client_name or "Cliente sem nome",
        escopo=ESCOPO_LEITURA,
        criada_por_id=usuario_id,
    )
    db.add(conexao)
    encontrado.usado_em = func.now()
    encontrado.conta_id = conta_id
    db.flush()
    resposta = servidor.create_authorization_response(requisicao, grant_user=conexao, grant=grant)
    destino = resposta.location
    db.commit()
    logger.info("conexao_mcp resultado=aprovada conexao_id=%s", conexao.id)
    return RedirecionamentoResponse(redirecionar_para=destino)


@router.post("/pedidos/{pedido}/recusar", response_model=RedirecionamentoResponse)
@limiter.limit(LIMITE_CONSENTIMENTO_MCP_POR_USUARIO, key_func=chave_por_usuario)
def recusar_pedido(
    request: Request,
    pedido: str,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> RedirecionamentoResponse:
    conta_id = usuario.conta_id
    encontrado = _pedido_pendente(db, pedido)
    destino = redirecionar_com_erro(encontrado.redirect_uri, "access_denied", encontrado.state)
    encontrado.usado_em = func.now()
    encontrado.conta_id = conta_id
    db.commit()
    logger.info("conexao_mcp resultado=recusada")
    return RedirecionamentoResponse(redirecionar_para=destino)


@router.get("", response_model=ConexaoListaResponse)
def listar_conexoes(
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> ConexaoListaResponse:
    criador = aliased(Usuario)
    linhas = db.execute(
        select(ConexaoMcp, Empresa.razao_social, criador.nome)
        .join(Empresa, Empresa.id == ConexaoMcp.empresa_id)
        .outerjoin(criador, criador.id == ConexaoMcp.criada_por_id)
        .where(ConexaoMcp.conta_id == usuario.conta_id)
        .order_by(ConexaoMcp.criado_em.desc(), ConexaoMcp.id)
    ).all()
    return ConexaoListaResponse(
        itens=[
            ConexaoResponse(
                id=conexao.id,
                cliente=conexao.nome_cliente,
                empresa=EmpresaDaConexao(id=conexao.empresa_id, razao_social=razao_social),
                criada_por=criada_por,
                criado_em=conexao.criado_em,
                ultimo_uso_em=conexao.ultimo_uso_em,
                revogada_em=conexao.revogada_em,
            )
            for conexao, razao_social, criada_por in linhas
        ]
    )


@router.delete("/{conexao_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def revogar(
    conexao_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    conexao = db.scalar(
        select(ConexaoMcp)
        .where(ConexaoMcp.id == conexao_id, ConexaoMcp.conta_id == usuario.conta_id)
        .with_for_update()
    )
    if conexao is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Conexão não encontrada."
        )
    if conexao.revogada_em is not None:
        db.rollback()
        return
    revogar_conexao(db, conexao.id, usuario.id)
    db.commit()
    logger.info("conexao_mcp resultado=revogada conexao_id=%s", conexao_id)
