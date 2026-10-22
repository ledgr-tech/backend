"""O servidor de autorização OAuth sobre o Authlib (issue #136, ADR-022).

O Authlib cuida do protocolo: validação do authorize, troca do código com
PKCE, renovação, registro dinâmico (RFC 7591), revogação (RFC 7009), o `iss`
na resposta (RFC 9207) e o formato dos erros (RFC 6749). Este módulo liga o
Authlib ao FastAPI e às tabelas de app/models/oauth.py:

- `RequisicaoOAuth` e `RequisicaoJson` montam a requisição do Authlib a
  partir dos dados já lidos pela rota. A URI usa `OAUTH_ISSUER`, e não a URL
  que chegou do proxy da Railway (http por dentro);
- `ServidorOAuth` é criado por requisição, com a sessão do banco dela;
- só cliente público (`none`), só `code` com PKCE S256 obrigatório, escopo
  único `ledgr.leitura` (mais `offline_access`, aceito e sem efeito: a
  renovação sai sempre);
- código de 60 segundos e uso único: reusar revoga os tokens emitidos a
  partir dele; token de acesso opaco `ledgr_mcp_...` de 1 hora e de
  renovação de 30 dias, com rotação: reusar um token de renovação já trocado
  revoga a conexão inteira (sinal de roubo).

Nenhum segredo é gravado (só sha256) nem logado.
"""

import hashlib
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import ClassVar

from authlib.common.urls import add_params_to_uri
from authlib.oauth2 import AuthorizationServer, OAuth2Error
from authlib.oauth2.rfc6749 import (
    InvalidRequestError,
    JsonPayload,
    JsonRequest,
    OAuth2Payload,
    OAuth2Request,
    grants,
)
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7591 import (
    ClientRegistrationEndpoint,
    InvalidClientMetadataError,
    InvalidRedirectURIError,
)
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc9207 import IssuerParameter
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ClienteOAuth, CodigoOAuth, ConexaoMcp, TokenOAuth
from app.services.conexoes_mcp import revogar_conexao
from app.services.oauth.redirects import mesmo_redirect, redirect_permitido

logger = logging.getLogger(__name__)

ESCOPO_LEITURA = "ledgr.leitura"
ESCOPO_RENOVACAO = "offline_access"
ESCOPOS_DO_SERVIDOR = (ESCOPO_LEITURA, ESCOPO_RENOVACAO)
GRANT_TYPES = ("authorization_code", "refresh_token")
PREFIXO_TOKEN_ACESSO = "ledgr_mcp_"
VALIDADE_CODIGO = timedelta(seconds=60)
VALIDADE_ACESSO = timedelta(hours=1)
VALIDADE_RENOVACAO = timedelta(days=30)
VALIDADE_CLIENTE_SEM_CONEXAO = timedelta(days=7)
TAMANHO_NOME_CLIENTE = 100


def hash_do_segredo(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()


def issuer() -> str:
    return settings.oauth_issuer.strip().rstrip("/")


def url_mcp() -> str:
    return settings.mcp_url_publica.strip().rstrip("/")


def configurado() -> bool:
    return bool(issuer() and url_mcp())


def mesmo_resource(valor: str | None) -> bool:
    """O `resource` é o servidor MCP? Esquema e host sem diferença de caixa,
    como a especificação recomenda, e sem a barra do fim."""
    if not valor:
        return False

    def _canonico(uri: str) -> str:
        esquema, _, resto = uri.strip().rstrip("/").partition("://")
        host, barra, caminho = resto.partition("/")
        return f"{esquema.lower()}://{host.lower()}{barra}{caminho}"

    return _canonico(valor) == _canonico(url_mcp())


class InvalidTargetError(OAuth2Error):
    """RFC 8707: o `resource` não é o servidor MCP."""

    error = "invalid_target"
    description = "The resource parameter must be the Ledgr MCP server."


# --- requisições -------------------------------------------------------------------------


class _Payload(OAuth2Payload):
    def __init__(self, dados: dict[str, list[str]]):
        self._lista = dados

    @property
    def data(self):
        return {chave: valores[0] for chave, valores in self._lista.items() if valores}

    @property
    def datalist(self):
        return defaultdict(list, self._lista)


class RequisicaoOAuth(OAuth2Request):
    def __init__(self, metodo: str, caminho: str, dados: dict[str, list[str]], headers=None):
        super().__init__(metodo, f"{issuer()}{caminho}", headers=dict(headers or {}))
        self.payload = _Payload(dados)

    @property
    def args(self):
        return self.payload.data

    @property
    def form(self):
        return self.payload.data


class _PayloadJson(JsonPayload):
    def __init__(self, dados: dict):
        self._dados = dados

    @property
    def data(self):
        return self._dados


class RequisicaoJson(JsonRequest):
    def __init__(self, caminho: str, dados: dict, headers=None):
        super().__init__("POST", f"{issuer()}{caminho}", dict(headers or {}))
        self.payload = _PayloadJson(dados)


@dataclass
class RespostaOAuth:
    status: int
    corpo: object
    cabecalhos: list[tuple[str, str]] = field(default_factory=list)

    @property
    def location(self) -> str | None:
        return dict(self.cabecalhos).get("Location")

    @location.setter
    def location(self, valor: str) -> None:
        self.cabecalhos = [(k, v) for k, v in self.cabecalhos if k != "Location"]
        self.cabecalhos.append(("Location", valor))


# --- modelos no formato do Authlib -------------------------------------------------------


@dataclass
class Cliente:
    """`ClientMixin` do Authlib sobre `ClienteOAuth`."""

    modelo: ClienteOAuth

    @property
    def client_id(self) -> str:
        return self.modelo.client_id

    @property
    def nome(self) -> str:
        return self.modelo.client_name or "Cliente sem nome"

    def get_client_id(self):
        return self.modelo.client_id

    def get_default_redirect_uri(self):
        return None  # o redirect_uri é obrigatório no authorize

    def get_allowed_scope(self, scope):
        pedidos = set((scope or "").split())
        concedidos = [ESCOPO_LEITURA]
        if ESCOPO_RENOVACAO in pedidos:
            concedidos.append(ESCOPO_RENOVACAO)
        return " ".join(concedidos)

    def check_redirect_uri(self, redirect_uri):
        return any(mesmo_redirect(redirect_uri, uri) for uri in self.modelo.redirect_uris)

    def check_client_secret(self, client_secret):
        return False

    def check_endpoint_auth_method(self, method, endpoint):
        return method == "none"

    def check_response_type(self, response_type):
        return response_type == "code"

    def check_grant_type(self, grant_type):
        return grant_type in GRANT_TYPES


@dataclass
class Codigo:
    """`AuthorizationCodeMixin` do Authlib sobre `CodigoOAuth`."""

    modelo: CodigoOAuth
    code_challenge_method: str = "S256"

    @property
    def code_challenge(self) -> str:
        return self.modelo.code_challenge

    def get_redirect_uri(self):
        return self.modelo.redirect_uri

    def get_scope(self):
        return self.modelo.scope


@dataclass
class Token:
    """`TokenMixin` do Authlib sobre `TokenOAuth`. `tipo` diz qual dos dois
    valores da linha foi apresentado."""

    modelo: TokenOAuth
    tipo: str

    def check_client(self, client):
        return self.modelo.client_id == client.get_client_id()

    def get_scope(self):
        return self.modelo.scope


# --- grants --------------------------------------------------------------------------------


class PkceS256(CodeChallenge):
    """PKCE obrigatório e só com S256."""

    DEFAULT_CODE_CHALLENGE_METHOD = "S256"
    SUPPORTED_CODE_CHALLENGE_METHOD: ClassVar[list[str]] = ["S256"]

    def validate_code_challenge(self, grant, redirect_uri):
        dados = grant.request.payload.data
        if not dados.get("code_challenge"):
            raise InvalidRequestError("Missing 'code_challenge'.")
        if dados.get("code_challenge_method") != "S256":
            raise InvalidRequestError("The 'code_challenge_method' must be S256.")
        super().validate_code_challenge(grant, redirect_uri)


class GrantCodigo(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS: ClassVar[list[str]] = ["none"]

    def save_authorization_code(self, code, request):
        conexao: ConexaoMcp = request.user
        dados = request.payload.data
        self.server.db.add(
            CodigoOAuth(
                hash=hash_do_segredo(code),
                conexao_id=conexao.id,
                client_id=request.client.get_client_id(),
                redirect_uri=dados["redirect_uri"],
                code_challenge=dados["code_challenge"],
                scope=request.scope,
                resource=dados["resource"],
                expira_em=func.now() + VALIDADE_CODIGO,
            )
        )
        self.server.db.flush()

    def query_authorization_code(self, code, client):
        db = self.server.db
        modelo = db.scalar(
            select(CodigoOAuth)
            .where(
                CodigoOAuth.hash == hash_do_segredo(code),
                CodigoOAuth.client_id == client.get_client_id(),
            )
            .with_for_update()
        )
        if modelo is None:
            return None
        if modelo.usado_em is not None:
            # RFC 6749 4.1.2: código reusado revoga o que saiu dele.
            db.execute(
                TokenOAuth.__table__.update()
                .where(TokenOAuth.codigo_id == modelo.id, TokenOAuth.revogado_em.is_(None))
                .values(revogado_em=func.now())
            )
            db.commit()
            logger.info("oauth_token resultado=codigo_reusado")
            return None
        if not db.scalar(
            select(CodigoOAuth.expira_em > func.now()).where(CodigoOAuth.id == modelo.id)
        ):
            return None
        return Codigo(modelo)

    def delete_authorization_code(self, authorization_code):
        authorization_code.modelo.usado_em = func.now()

    def authenticate_user(self, authorization_code):
        conexao = self.server.db.get(ConexaoMcp, authorization_code.modelo.conexao_id)
        return conexao if conexao is not None and conexao.revogada_em is None else None


class GrantRenovacao(grants.RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS: ClassVar[list[str]] = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    def validate_token_request(self):
        super().validate_token_request()
        resource = self.request.form.get("resource")
        if resource is not None and not mesmo_resource(resource):
            raise InvalidTargetError()

    def authenticate_refresh_token(self, refresh_token):
        db = self.server.db
        modelo = db.scalar(
            select(TokenOAuth)
            .where(TokenOAuth.hash_renovacao == hash_do_segredo(refresh_token))
            .with_for_update()
        )
        if modelo is None:
            return None
        if modelo.renovado_em is not None:
            # Já trocado: alguém guardou o antigo. Corta a conexão inteira.
            revogar_conexao(db, modelo.conexao_id)
            db.commit()
            logger.info("oauth_token resultado=renovacao_reusada")
            return None
        valido = db.scalar(
            select(TokenOAuth.renovacao_expira_em > func.now()).where(TokenOAuth.id == modelo.id)
        )
        conexao = db.get(ConexaoMcp, modelo.conexao_id)
        if not valido or modelo.revogado_em is not None or conexao.revogada_em is not None:
            return None
        return Token(modelo, "renovacao")

    def authenticate_user(self, refresh_token):
        return self.server.db.get(ConexaoMcp, refresh_token.modelo.conexao_id)

    def revoke_old_credential(self, refresh_token):
        refresh_token.modelo.renovado_em = func.now()


def _conferir_resource_do_codigo(grant, resultado):
    """Depois do PKCE: o `resource` do pedido de token, quando vem, é o do
    código (que é o servidor MCP)."""
    resource = grant.request.form.get("resource")
    if resource is not None and not mesmo_resource(resource):
        raise InvalidTargetError()


class _CodigoComResource:
    def __call__(self, grant):
        grant.register_hook("after_validate_token_request", _conferir_resource_do_codigo)


# --- endpoints -----------------------------------------------------------------------------


class Revogacao(RevocationEndpoint):
    CLIENT_AUTH_METHODS: ClassVar[list[str]] = ["none"]

    def query_token(self, token_string, token_type_hint):
        db = self.server.db
        valor = hash_do_segredo(token_string)
        modelo = db.scalar(select(TokenOAuth).where(TokenOAuth.hash_acesso == valor))
        if modelo is not None:
            return Token(modelo, "acesso")
        modelo = db.scalar(select(TokenOAuth).where(TokenOAuth.hash_renovacao == valor))
        return Token(modelo, "renovacao") if modelo is not None else None

    def revoke_token(self, token, request):
        """O de renovação revoga a conexão; o de acesso só vence na hora, e a
        renovação daquela emissão continua valendo (RFC 7009 2.1)."""
        if token.tipo == "renovacao":
            revogar_conexao(self.server.db, token.modelo.conexao_id)
        else:
            token.modelo.acesso_expira_em = func.now()


class Registro(ClientRegistrationEndpoint):
    def authenticate_token(self, request):
        return True  # registro aberto; os redirect_uris é que são restritos

    def get_server_metadata(self):
        return metadados_do_servidor()

    def extract_client_metadata(self, request):
        dados = request.payload.data
        if not isinstance(dados, dict) or not dados:
            raise InvalidClientMetadataError("The body must be a JSON object.")
        # Só cliente público: sem o campo, vale `none`, e não o padrão do RFC.
        dados.setdefault("token_endpoint_auth_method", "none")
        dados.setdefault("grant_types", list(GRANT_TYPES))
        dados.setdefault("response_types", ["code"])
        # Antes da validação do Authlib: todo redirect recusado é
        # `invalid_redirect_uri`, mesmo o que nem é URL.
        uris = dados.get("redirect_uris")
        if not uris or not isinstance(uris, list):
            raise InvalidRedirectURIError("At least one redirect_uri is required.")
        if not all(isinstance(uri, str) and redirect_permitido(uri) for uri in uris):
            raise InvalidRedirectURIError("redirect_uri not allowed by this server.")
        metadados = super().extract_client_metadata(request)
        nome = metadados.get("client_name")
        if nome is not None and (not isinstance(nome, str) or len(nome) > TAMANHO_NOME_CLIENTE):
            raise InvalidClientMetadataError("client_name must have up to 100 characters.")
        return metadados

    def generate_client_info(self, request):
        return {
            "client_id": secrets.token_urlsafe(24),
            "client_id_issued_at": int(time.time()),
        }

    def save_client(self, client_info, client_metadata, request):
        db = self.server.db
        db.execute(
            ClienteOAuth.__table__.delete().where(
                ClienteOAuth.criado_em < func.now() - VALIDADE_CLIENTE_SEM_CONEXAO,
                ~exists().where(ConexaoMcp.client_id == ClienteOAuth.client_id),
            )
        )
        modelo = ClienteOAuth(
            client_id=client_info["client_id"],
            client_name=(client_metadata.get("client_name") or "").strip() or None,
            redirect_uris=list(client_metadata["redirect_uris"]),
        )
        db.add(modelo)
        db.flush()
        return Cliente(modelo)


class Issuer(IssuerParameter):
    def get_issuer(self):
        return issuer()


# --- servidor ------------------------------------------------------------------------------


def _gerar_token(
    grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True
):
    token = {
        "token_type": "Bearer",
        "access_token": PREFIXO_TOKEN_ACESSO + secrets.token_urlsafe(32),
        "expires_in": int(VALIDADE_ACESSO.total_seconds()),
        "scope": scope or ESCOPO_LEITURA,
    }
    if include_refresh_token:
        token["refresh_token"] = secrets.token_urlsafe(32)
    return token


class ServidorOAuth(AuthorizationServer):
    def __init__(self, db: Session):
        super().__init__(scopes_supported=list(ESCOPOS_DO_SERVIDOR))
        self.db = db
        self.register_token_generator("default", _gerar_token)
        self.register_grant(GrantCodigo, [PkceS256(required=True), _CodigoComResource()])
        self.register_grant(GrantRenovacao)
        self.register_endpoint(Revogacao)
        self.register_endpoint(Registro)
        self.register_extension(Issuer())

    def query_client(self, client_id):
        modelo = self.db.get(ClienteOAuth, client_id) if client_id else None
        return Cliente(modelo) if modelo is not None else None

    def save_token(self, token, request):
        if request.authorization_code is not None:
            codigo = request.authorization_code.modelo
            codigo_id, resource = codigo.id, codigo.resource
        else:
            anterior = request.refresh_token.modelo
            codigo_id, resource = anterior.codigo_id, anterior.resource
        self.db.add(
            TokenOAuth(
                conexao_id=request.user.id,
                codigo_id=codigo_id,
                client_id=request.client.get_client_id(),
                hash_acesso=hash_do_segredo(token["access_token"]),
                hash_renovacao=(
                    hash_do_segredo(token["refresh_token"]) if "refresh_token" in token else None
                ),
                scope=token["scope"],
                resource=resource,
                acesso_expira_em=func.now() + VALIDADE_ACESSO,
                renovacao_expira_em=(
                    func.now() + VALIDADE_RENOVACAO if "refresh_token" in token else None
                ),
            )
        )

    def create_oauth2_request(self, request):
        return request

    def create_json_request(self, request):
        return request

    def handle_response(self, status, body, headers):
        return RespostaOAuth(status, body, list(headers))

    def send_signal(self, name, *args, **kwargs):
        pass


def redirecionar_com_erro(redirect_uri: str, erro: str, state: str | None) -> str:
    """O redirect de erro do authorize, com `state` e `iss` (RFC 9207)."""
    params = [("error", erro)]
    if state:
        params.append(("state", state))
    params.append(("iss", issuer()))
    return add_params_to_uri(redirect_uri, params)


def metadados_do_servidor() -> dict:
    """RFC 8414. Sem `client_id_metadata_document_supported`: o CIMD fica
    para depois (ADR-022)."""
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": list(GRANT_TYPES),
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(ESCOPOS_DO_SERVIDOR),
        "authorization_response_iss_parameter_supported": True,
    }


def metadados_do_recurso() -> dict:
    """RFC 9728. Sem `offline_access`: renovação não é requisito do recurso."""
    return {
        "resource": url_mcp(),
        "authorization_servers": [issuer()],
        "scopes_supported": [ESCOPO_LEITURA],
        "bearer_methods_supported": ["header"],
        "resource_name": "Ledgr",
    }


def novo_id_aleatorio() -> str:
    return secrets.token_urlsafe(32)
