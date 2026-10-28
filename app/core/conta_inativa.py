"""Conta inativa, somente leitura (issue #156, ADR-025).

O operador do Ledgr inativa a conta pelo `/admin` (`contas.inativada_em`).
Daí em diante:

- login, `GET /me` e toda leitura dos próprios dados continuam, inclusive as
  exportações;
- toda escrita dá 403 `{"detail": MENSAGEM, "codigo": "conta_inativa"}`,
  menos as de `LIBERADAS`: as da própria pessoa (senha, e-mail, preferências,
  favoritos, desativar a própria conta) e as quatro revogações (chave de
  API, conexão MCP, link público e usuário), que só reduzem acesso;
- a API pública (`/v1`), o MCP e o `/oauth/token` param, e os links
  públicos dão o 404 dos inválidos;
- a geração automática de explicações para antes da próxima linha.

A regra está em um ponto por porta de entrada, nunca rota por rota:
`app.core.auth.obter_sessao_autenticada` (toda rota do app com JWT),
`obter_chave_api` (`/v1`), `autenticar_token_mcp` (MCP), os grants de
app/services/oauth/servidor.py, o aceite de convite (público),
`app.services.relatorio_publico.link_valido` e
`app.services.ia.geracao._gerar_em_sessao_propria`. O `/admin` fica de fora:
não depende da conta do operador.

`ContaInativaError` vira a resposta 403 pelo handler registrado em main.py.
"""

import uuid

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

CODIGO = "conta_inativa"
MENSAGEM = (
    "Esta conta está inativa. Você pode consultar e exportar os dados, mas não pode fazer "
    "alterações. Fale com o Ledgr para reativar."
)
METODOS_DE_LEITURA = frozenset({"GET", "HEAD", "OPTIONS"})
# Escritas que seguem com a conta inativa: (método, modelo do caminho).
LIBERADAS = frozenset(
    {
        # Da própria pessoa, não dos dados da conta.
        ("POST", "/me/senha"),
        ("POST", "/me/email"),
        ("PATCH", "/me/preferencias"),
        ("DELETE", "/me"),
        ("PUT", "/empresas/{empresa_id}/favorita"),
        ("DELETE", "/empresas/{empresa_id}/favorita"),
        # Revogações: só reduzem acesso. Bloqueadas, uma chave vazada voltaria
        # a valer na reativação.
        ("DELETE", "/chaves-api/{chave_id}"),
        ("DELETE", "/conexoes-mcp/{conexao_id}"),
        ("DELETE", "/fechamentos/{competencia}/links/{link_id}"),
        ("DELETE", "/empresa/usuarios/{usuario_id}"),
    }
)
PREFIXO_ADMIN = "/admin"


class ContaInativaError(Exception):
    """A conta está inativa e a operação escreve."""


def corpo() -> dict:
    return {"detail": MENSAGEM, "codigo": CODIGO}


def resposta() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content=corpo())


async def tratar(_request: Request, _exc: ContaInativaError) -> JSONResponse:
    return resposta()


def _modelo_do_caminho(request: Request) -> str:
    rota = request.scope.get("route")
    return getattr(rota, "path", None) or request.url.path


def escrita_bloqueada(request: Request) -> bool:
    """A requisição é uma escrita que a conta inativa não pode fazer."""
    if request.method in METODOS_DE_LEITURA:
        return False
    caminho = _modelo_do_caminho(request)
    if caminho == PREFIXO_ADMIN or caminho.startswith(PREFIXO_ADMIN + "/"):
        return False
    return (request.method, caminho) not in LIBERADAS


def conta_esta_inativa(db: Session, conta_id: uuid.UUID) -> bool:
    from app.models import Conta

    return db.scalar(select(Conta.inativada_em).where(Conta.id == conta_id)) is not None


def conta_da_empresa_esta_inativa(db: Session, empresa_id: uuid.UUID) -> bool:
    from app.models import Conta, Empresa

    return (
        db.scalar(
            select(Conta.inativada_em)
            .join(Empresa, Empresa.conta_id == Conta.id)
            .where(Empresa.id == empresa_id)
        )
        is not None
    )
