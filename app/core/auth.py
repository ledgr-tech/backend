"""Autenticação por JWT (ADR-003) — interface entre backend e frontend.

Contrato do token, emitido pelo NextAuth no frontend (outro repo):
- Algoritmo HS256, assinado com NEXTAUTH_SECRET (segredo compartilhado).
- Claims: `sub` (usuario_id, string), `conta_id` (string UUID), `email`,
  `iat`, `exp` e, desde a issue #75, `auth_time`: o momento do login, em
  segundos inteiros desde a época, gravado só no login e mantido em cada
  renovação da sessão. O valor vem da resposta do `POST /login`
  (`instante_do_login`), e não do relógio do front.
- Enviado em `Authorization: Bearer <token>`.

Tenant (issue #133, ADR-019 e vetor v3 do ADR-005): o claim é `conta_id`.
Até o front migrar, o legado `empresa_id` continua aceito quando `conta_id`
não vem, e os dois são lidos como o id da conta: a migração da #133 criou
cada conta com o mesmo id da empresa de que veio, então o token emitido antes
dela continua valendo. Token com os dois claims e valores diferentes é 401.

Toda rota autenticada passa por `_sessao_do_token` (issue #75, ADR-016),
que exige `exp` e `auth_time`, carrega o usuário do `sub` por chave primária
e só deixa passar quando:

- o usuário existe, não foi removido (issue #69) e é da conta do token;
- `auth_time` não está no futuro nem tem mais de `DURACAO_SESSAO_SEGUNDOS`:
  a sessão dura 7 dias desde o login (ADR-003), mesmo que o front renove o
  token com `iat` e `exp` novos;
- `auth_time` não é anterior a `usuarios.sessoes_validas_desde`, que
  `encerrar_sessoes` grava na troca de senha, na redefinição pelo link e na
  remoção do usuário.

Um relógio só: o `auth_time` do login, o `sessoes_validas_desde` e o
"agora" das comparações saem todos de `_agora`, o relógio deste processo.
Com o `auth_time` calculado pela Vercel e o `sessoes_validas_desde` pelo
Postgres, um relógio um pouco atrasado em relação ao outro faria o login
logo depois da troca de senha sair com `auth_time` um segundo antes da
troca, e a sessão nasceria inválida para sempre (a renovação mantém o
`auth_time`).

Qualquer falha vira o mesmo 401, sem detalhar o motivo. As chaves do rate
limit (app/core/rate_limit.py) continuam sem banco.

Dependencies, todas a partir de `obter_sessao_autenticada`, que o FastAPI
resolve uma vez por requisição:

- `obter_sessao_autenticada` devolve o usuário e o `auth_time` do token, para
  quem precisa saber se o login é recente (`login_recente`): a desativação da
  conta só do Google, que não tem senha para conferir (issue #68);
- `obter_usuario_autenticado` devolve o usuário, para as rotas da conta
  (`/me/*`, convites, usuários e plano), que ignoram a empresa ativa;
- `obter_empresa_ativa` devolve o usuário e a empresa ativa, e
  `obter_empresa_id_ativa`, só o id dela, para as rotas por empresa
  (extratos, conciliações, execuções, explicações, fechamentos e
  configuração). As duas saem de `_empresa_ativa`, para nunca divergirem;
- `exigir_papel(*papeis)` deixa passar só os papéis dados e devolve 403 aos
  outros (issue #133, ADR-019); `obter_admin_autenticado` é o caso do admin.

Chave da API pública (issue #135, ADR-021): `obter_chave_api` é a outra porta
de entrada, ao lado do NextAuth, só para as rotas `/v1`. O token
(`Authorization: Bearer ledgr_...`) tem de começar com `PREFIXO_CHAVE_API`; a
chave é achada pelo sha256 dele e não pode estar revogada. Qualquer falha é o
mesmo 401 "Chave de API inválida.". O JWT do app não começa com o prefixo, e
falha nas `/v1`; a chave não é um JWT, e falha nas rotas do app como token
malformado. O `ultimo_uso_em` é gravado numa `BackgroundTask`, no máximo uma
vez por minuto por chave, depois da resposta.

Token da conexão MCP (issue #136, ADR-022): `obter_conexao_mcp` é a terceira
porta, só para o servidor MCP (/mcp, parte 2). O token de acesso é opaco
(`ledgr_mcp_...`), emitido pelo servidor de autorização do próprio Ledgr
(app/api/oauth.py), e é achado pelo sha256. Precisa estar no prazo, com a
conexão não revogada e com o `resource` igual a `MCP_URL_PUBLICA`. A falha é
401 com o `WWW-Authenticate` que manda o cliente à descoberta OAuth. O JWT do
app e a chave de API nunca valem ali, e este token nunca vale nas outras
rotas (não é JWT, e o hash não está em `chaves_api`).

Empresa ativa (issue #133, ADR-019): vem no cabeçalho `X-Ledgr-Empresa`, com o
uuid da empresa, e é conferida contra a conta do usuário em toda requisição,
para duas abas com clientes diferentes nunca gravarem no cliente errado.

- Sem o cabeçalho, numa conta com uma empresa só, vale essa (o front de antes
  da #133 não manda nada). Com mais de uma, 400 "Escolha a empresa.".
- Valor que não é UUID: 400.
- UUID inexistente ou de outra conta: 404 "Empresa não encontrada.", a mesma
  mensagem nos dois casos.
"""

import hashlib
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.models import ChaveApi, ConexaoMcp, Empresa, TokenOAuth, Usuario
from app.models.usuario import Papel

logger = logging.getLogger(__name__)

ALGORITMO_JWT = "HS256"

# ADR-003: a sessão vale 7 dias desde o login, contados do `auth_time`.
# O front usa o mesmo número (`DURACAO_SESSAO_SEGUNDOS` em lib/token.ts).
DURACAO_SESSAO_SEGUNDOS = 7 * 24 * 60 * 60

# Login recente (issue #68): o que uma ação sensível sem senha para conferir
# exige, contado do `auth_time` no mesmo relógio de `_agora`.
LOGIN_RECENTE_SEGUNDOS = 5 * 60

# Empresa ativa da requisição (issue #133, ADR-019).
CABECALHO_EMPRESA = "X-Ledgr-Empresa"

_bearer = HTTPBearer(auto_error=False)


def _agora() -> datetime:
    return datetime.now(UTC)


def instante_do_login() -> int:
    """O `auth_time` de um login cuja senha acabou de ser conferida: agora,
    em segundos inteiros desde a época, no mesmo relógio das comparações."""
    return math.floor(_agora().timestamp())


def normalizar_email(email: str) -> str:
    """Forma única do e-mail: a que fica no banco e a chave do bloqueio de login.

    Mora aqui, e não em app/api/auth.py, para app/core/bloqueio_login.py usar
    sem importar a camada de rotas (issue #67)."""
    return email.strip().lower()


def _nao_autenticado() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _claims_do_token(credenciais: HTTPAuthorizationCredentials | None) -> dict:
    if credenciais is None:
        raise _nao_autenticado()

    secret = settings.exigir_nextauth_secret()
    try:
        return jwt.decode(
            credenciais.credentials,
            secret,
            algorithms=[ALGORITMO_JWT],
            options={"require": ["exp", "auth_time"]},
        )
    except jwt.PyJWTError as exc:
        raise _nao_autenticado() from exc


def _uuid_do_claim(claims: dict, nome: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(claims[nome]))
    except (KeyError, ValueError) as exc:
        raise _nao_autenticado() from exc


def conta_id_do_claim(claims: dict) -> str | None:
    """O id da conta do token, como texto: o claim `conta_id` ou, sem ele, o
    legado `empresa_id` (issue #133). `None` sem nenhum dos dois, ou com os
    dois e valores diferentes. Não confere o formato. Também usada pelo rate
    limit, que não consulta o banco."""
    conta_id, legado = claims.get("conta_id"), claims.get("empresa_id")
    if conta_id is None:
        return None if legado is None else str(legado)
    if legado is not None and str(legado) != str(conta_id):
        return None
    return str(conta_id)


def _conta_do_token(claims: dict) -> uuid.UUID:
    valor = conta_id_do_claim(claims)
    if valor is None:
        raise _nao_autenticado()
    try:
        return uuid.UUID(valor)
    except ValueError as exc:
        raise _nao_autenticado() from exc


def _auth_time(claims: dict) -> int:
    """O `auth_time` do token, só se for inteiro e não estiver no futuro.

    Sem folga: o valor vem do `POST /login`, calculado por `_agora`, o mesmo
    relógio desta comparação."""
    valor = claims["auth_time"]
    # bool é subclasse de int no Python, e `true` não é um instante
    if type(valor) is not int or valor > _agora().timestamp():
        raise _nao_autenticado()
    return valor


def _sessao_valida(usuario: Usuario, auth_time: int) -> bool:
    if _agora().timestamp() - auth_time > DURACAO_SESSAO_SEGUNDOS:
        return False
    if usuario.sessoes_validas_desde is None:
        return True
    return auth_time >= math.floor(usuario.sessoes_validas_desde.timestamp())


def login_recente(auth_time: int) -> bool:
    """O login do `auth_time` foi há no máximo `LOGIN_RECENTE_SEGUNDOS`."""
    return _agora().timestamp() - auth_time <= LOGIN_RECENTE_SEGUNDOS


@dataclass(frozen=True)
class SessaoAutenticada:
    usuario: Usuario
    auth_time: int


def _sessao_do_token(
    credenciais: HTTPAuthorizationCredentials | None, db: Session
) -> SessaoAutenticada:
    claims = _claims_do_token(credenciais)
    usuario_id = _uuid_do_claim(claims, "sub")
    conta_id = _conta_do_token(claims)
    auth_time = _auth_time(claims)
    usuario = db.get(Usuario, usuario_id)
    if (
        usuario is None
        or usuario.removido_em is not None
        or usuario.conta_id != conta_id
        or not _sessao_valida(usuario, auth_time)
    ):
        raise _nao_autenticado()
    return SessaoAutenticada(usuario=usuario, auth_time=auth_time)


def encerrar_sessoes(usuario: Usuario) -> None:
    """Invalida todo token emitido antes de agora, inclusive o de quem chamou.

    Truncado no segundo porque o `auth_time` é inteiro: sem isso, o login
    feito logo depois, ainda no mesmo segundo, sairia com `auth_time`
    anterior ao valor gravado e daria 401. Vem de `_agora`, o relógio do
    `instante_do_login`, e não do `now()` do Postgres. Sem commit: vale com a
    transação de quem chama."""
    usuario.sessoes_validas_desde = _agora().replace(microsecond=0)


def obter_sessao_autenticada(
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> SessaoAutenticada:
    return _sessao_do_token(credenciais, db)


def obter_usuario_autenticado(
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
) -> Usuario:
    return sessao.usuario


def _erro(status: int, detalhe: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detalhe)


def _empresa_ativa(db: Session, usuario: Usuario, cabecalho: str | None) -> Empresa:
    """A empresa ativa da requisição, conferida contra a conta do usuário. As
    regras estão no docstring do módulo."""
    if cabecalho is None:
        empresas = db.scalars(
            select(Empresa).where(Empresa.conta_id == usuario.conta_id).limit(2)
        ).all()
        if len(empresas) > 1:
            raise _erro(http_status.HTTP_400_BAD_REQUEST, "Escolha a empresa.")
        if not empresas:
            raise _erro(http_status.HTTP_404_NOT_FOUND, "Empresa não encontrada.")
        return empresas[0]
    try:
        empresa_id = uuid.UUID(cabecalho.strip())
    except ValueError as exc:
        raise _erro(
            http_status.HTTP_400_BAD_REQUEST, f"{CABECALHO_EMPRESA} não é um id de empresa."
        ) from exc
    empresa = db.get(Empresa, empresa_id)
    if empresa is None or empresa.conta_id != usuario.conta_id:
        raise _erro(http_status.HTTP_404_NOT_FOUND, "Empresa não encontrada.")
    return empresa


@dataclass(frozen=True)
class EmpresaAtiva:
    usuario: Usuario
    empresa: Empresa


def obter_empresa_ativa(
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    x_ledgr_empresa: Annotated[
        str | None,
        Header(
            alias=CABECALHO_EMPRESA,
            description=(
                "Id da empresa ativa. Opcional só quando a conta tem uma empresa "
                "(issue #133, ADR-019)."
            ),
        ),
    ] = None,
) -> EmpresaAtiva:
    usuario = sessao.usuario
    return EmpresaAtiva(usuario=usuario, empresa=_empresa_ativa(db, usuario, x_ledgr_empresa))


def obter_empresa_id_ativa(
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
) -> uuid.UUID:
    return ativa.empresa.id


# O que vai no 403 de `exigir_papel`, por papel.
_NOME_DO_PAPEL = {"admin": "o admin", "gestor": "o gestor", "analista": "o analista"}


def mensagem_de_papel(papeis: tuple[Papel, ...]) -> str:
    """O `detail` do 403 de quem não tem um dos `papeis`."""
    quem = " ou ".join(_NOME_DO_PAPEL[papel] for papel in papeis)
    verbo = "pode" if len(papeis) == 1 else "podem"
    return f"Só {quem} da conta {verbo} fazer isso."


def exigir_papel(*papeis: Papel):
    """Dependency que só deixa passar os `papeis` dados; os outros recebem 403
    com quem pode (issue #133, ADR-019). Vale para a conta toda, sem olhar a
    empresa ativa: a v1 não tem permissão por empresa."""
    if not papeis:
        raise ValueError("exigir_papel precisa de pelo menos um papel")
    detalhe = mensagem_de_papel(papeis)

    def _usuario_com_papel(
        usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    ) -> Usuario:
        if usuario.papel not in papeis:
            raise _erro(http_status.HTTP_403_FORBIDDEN, detalhe)
        return usuario

    return _usuario_com_papel


# Convites, remoção de usuário e plano (issue #69, ADR-015; papel desde a #133).
obter_admin_autenticado = exigir_papel("admin")


# --- chave da API pública (issue #135, ADR-021) ------------------------------------

PREFIXO_CHAVE_API = "ledgr_"
# O `ultimo_uso_em` é gravado no máximo uma vez por este intervalo, por chave.
INTERVALO_ULTIMO_USO = timedelta(minutes=1)


def hash_da_chave_api(token: str) -> str:
    """O sha256 hex do token inteiro: o que fica no banco e a chave do rate
    limit, que não consulta o banco."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class ChaveAutenticada:
    chave_id: uuid.UUID
    prefixo: str
    conta_id: uuid.UUID
    # Nulo: todas as empresas da conta; preenchido: só esta.
    empresa_id: uuid.UUID | None


def _chave_invalida() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Chave de API inválida.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def registrar_uso_da_chave(chave_id: uuid.UUID) -> None:
    """Grava o último uso numa sessão própria, se o anterior tem mais de um
    minuto. Nunca levanta: o uso é só informativo."""
    try:
        with SessionLocal() as db:
            db.execute(
                update(ChaveApi)
                .where(
                    ChaveApi.id == chave_id,
                    or_(
                        ChaveApi.ultimo_uso_em.is_(None),
                        ChaveApi.ultimo_uso_em < _agora() - INTERVALO_ULTIMO_USO,
                    ),
                )
                .values(ultimo_uso_em=_agora())
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 - o último uso nunca derruba nada
        logger.info("chave_api ultimo_uso_falhou classe=%s", type(exc).__name__)


def obter_chave_api(
    request: Request,
    tarefas: BackgroundTasks,
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> ChaveAutenticada:
    if credenciais is None or not credenciais.credentials.startswith(PREFIXO_CHAVE_API):
        raise _chave_invalida()
    chave = db.scalar(
        select(ChaveApi).where(ChaveApi.hash == hash_da_chave_api(credenciais.credentials))
    )
    if chave is None or chave.revogada_em is not None:
        raise _chave_invalida()
    autenticada = ChaveAutenticada(
        chave_id=chave.id,
        prefixo=chave.prefixo,
        conta_id=chave.conta_id,
        empresa_id=chave.empresa_id,
    )
    # Para o log de acesso das /v1 (app/api/documentacao_publica.py).
    request.state.chave_api = autenticada
    ultimo = chave.ultimo_uso_em
    if ultimo is None or _agora() - ultimo >= INTERVALO_ULTIMO_USO:
        tarefas.add_task(registrar_uso_da_chave, chave.id)
    return autenticada


# --- token da conexão MCP (issue #136, ADR-022) ------------------------------------

PREFIXO_TOKEN_MCP = "ledgr_mcp_"
ESCOPO_MCP = "ledgr.leitura"


@dataclass(frozen=True)
class ConexaoAutenticada:
    conexao_id: uuid.UUID
    conta_id: uuid.UUID
    # A conexão vale para uma empresa só, escolhida no consentimento.
    empresa_id: uuid.UUID
    client_id: str
    escopo: str


def _token_mcp_invalido() -> HTTPException:
    base = settings.oauth_issuer.strip().rstrip("/")
    return HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Token de acesso inválido.",
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource", '
                f'scope="{ESCOPO_MCP}"'
            )
        },
    )


def registrar_uso_da_conexao(conexao_id: uuid.UUID) -> None:
    """Como `registrar_uso_da_chave`: no máximo uma vez por minuto, numa
    sessão própria, depois da resposta, e sem nunca levantar."""
    try:
        with SessionLocal() as db:
            db.execute(
                update(ConexaoMcp)
                .where(
                    ConexaoMcp.id == conexao_id,
                    or_(
                        ConexaoMcp.ultimo_uso_em.is_(None),
                        ConexaoMcp.ultimo_uso_em < _agora() - INTERVALO_ULTIMO_USO,
                    ),
                )
                .values(ultimo_uso_em=_agora())
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 - o último uso nunca derruba nada
        logger.info("conexao_mcp ultimo_uso_falhou classe=%s", type(exc).__name__)


def obter_conexao_mcp(
    tarefas: BackgroundTasks,
    credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> ConexaoAutenticada:
    if not settings.mcp_habilitado:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Not Found")
    recurso = settings.mcp_url_publica.strip().rstrip("/")
    if not recurso or not settings.oauth_issuer.strip():
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conexão com assistentes de IA indisponível no momento.",
        )
    if credenciais is None or not credenciais.credentials.startswith(PREFIXO_TOKEN_MCP):
        raise _token_mcp_invalido()
    hash_do_token = hashlib.sha256(credenciais.credentials.encode()).hexdigest()
    linha = db.execute(
        select(TokenOAuth, ConexaoMcp)
        .join(ConexaoMcp, ConexaoMcp.id == TokenOAuth.conexao_id)
        .where(
            TokenOAuth.hash_acesso == hash_do_token,
            TokenOAuth.revogado_em.is_(None),
            TokenOAuth.acesso_expira_em > _agora(),
            ConexaoMcp.revogada_em.is_(None),
        )
    ).first()
    if linha is None or linha[0].resource != recurso:
        raise _token_mcp_invalido()
    token, conexao = linha
    ultimo = conexao.ultimo_uso_em
    if ultimo is None or _agora() - ultimo >= INTERVALO_ULTIMO_USO:
        tarefas.add_task(registrar_uso_da_conexao, conexao.id)
    return ConexaoAutenticada(
        conexao_id=conexao.id,
        conta_id=conexao.conta_id,
        empresa_id=conexao.empresa_id,
        client_id=conexao.client_id,
        escopo=token.scope,
    )
