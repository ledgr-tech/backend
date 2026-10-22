"""Servidor de autorização OAuth e conexões MCP (issue #136, parte 1; ADR-022).

O cliente de teste faz o que o ChatGPT e o Claude fazem: registro dinâmico,
authorize com PKCE S256 e `resource`, consentimento pela tela do front
(aqui, as rotas do app com o JWT), troca do código, renovação com rotação e
o uso do token de acesso, pela dependency `obter_conexao_mcp` montada numa
app de teste (o /mcp é a parte 2). Nenhum teste acessa a internet.
"""

import base64
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FRONTEND_URL_TESTE, cabecalhos, empresa_unica
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core import auth
from app.core.auth import ConexaoAutenticada, obter_conexao_mcp
from app.core.config import settings
from app.models import ClienteOAuth, CodigoOAuth, ConexaoMcp, TokenOAuth
from main import app

client = TestClient(app)

ISSUER = "https://api.ledgr.teste"
RECURSO = "https://api.ledgr.teste/mcp"
CALLBACK_CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CALLBACK_CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"

# O /mcp é a parte 2: uma app só com a dependency.
app_mcp = FastAPI()


@app_mcp.get("/mcp-teste")
def _rota_mcp(conexao: Annotated[ConexaoAutenticada, Depends(obter_conexao_mcp)]) -> dict:
    return {
        "conexao_id": str(conexao.conexao_id),
        "conta_id": str(conexao.conta_id),
        "empresa_id": str(conexao.empresa_id),
        "escopo": conexao.escopo,
    }


client_mcp = TestClient(app_mcp)


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(settings, "mcp_habilitado", True)
    monkeypatch.setattr(settings, "mcp_url_publica", RECURSO)
    monkeypatch.setattr(settings, "oauth_issuer", ISSUER)
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _hash(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()


def registrar(redirect=CALLBACK_CLAUDE, nome="Claude", **extra):
    corpo = {
        "client_name": nome,
        "redirect_uris": [redirect] if isinstance(redirect, str) else redirect,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **extra,
    }
    return client.post("/oauth/register", json=corpo)


def autorizar(client_id, redirect=CALLBACK_CLAUDE, challenge=None, **params):
    base = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "estado-do-cliente",
        "scope": "ledgr.leitura offline_access",
        "resource": RECURSO,
    }
    base.update(params)
    return client.get(
        "/oauth/authorize",
        params={k: v for k, v in base.items() if v is not None},
        follow_redirects=False,
    )


def trocar_codigo(client_id, code, verifier, redirect=CALLBACK_CLAUDE, **params):
    corpo = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect,
        "resource": RECURSO,
        **params,
    }
    return client.post("/oauth/token", data={k: v for k, v in corpo.items() if v is not None})


def renovar(client_id, refresh_token, **params):
    corpo = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "resource": RECURSO,
        **params,
    }
    return client.post("/oauth/token", data={k: v for k, v in corpo.items() if v is not None})


def usar(access_token: str):
    return client_mcp.get("/mcp-teste", headers={"Authorization": f"Bearer {access_token}"})


@pytest.fixture
def conta(db_session, criar_usuario, criar_empresa, gerar_token):
    """Conta com admin e fábricas para o fluxo inteiro."""

    def _nova():
        admin = criar_usuario(nome="Ana Admin")
        empresa_id = empresa_unica(db_session, admin.conta_id)

        def cabecalho(usuario=admin):
            return cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id))

        def com_papel(papel):
            return criar_usuario(conta_id=admin.conta_id, papel=papel, nome=f"Pessoa {papel}")

        def outra_empresa():
            return criar_empresa(razao_social="Segunda Ltda", conta_id=admin.conta_id).id

        def pedido(redirect=CALLBACK_CLAUDE, nome="Claude"):
            registro = registrar(redirect, nome)
            assert registro.status_code == 201, registro.text
            client_id = registro.json()["client_id"]
            verifier, challenge = _pkce()
            resposta = autorizar(client_id, redirect, challenge)
            assert resposta.status_code == 302, resposta.text
            destino = resposta.headers["location"]
            assert destino.startswith(f"{FRONTEND_URL_TESTE}/autorizar-conexao?pedido=")
            return client_id, verifier, _query(destino)["pedido"]

        def aprovar(pedido_id, empresa=None, usuario=admin):
            return client.post(
                f"/conexoes-mcp/pedidos/{pedido_id}/aprovar",
                headers=cabecalho(usuario),
                json={"empresa_id": str(empresa or empresa_id)},
            )

        def conectar(empresa=None, usuario=admin, redirect=CALLBACK_CLAUDE):
            """Do registro ao token. Devolve (client_id, corpo do token)."""
            client_id, verifier, pedido_id = pedido(redirect)
            aprovada = aprovar(pedido_id, empresa, usuario)
            assert aprovada.status_code == 200, aprovada.text
            code = _query(aprovada.json()["redirecionar_para"])["code"]
            tokens = trocar_codigo(client_id, code, verifier, redirect)
            assert tokens.status_code == 200, tokens.text
            return client_id, tokens.json()

        return _Conta(
            admin=admin,
            empresa_id=empresa_id,
            cabecalho=cabecalho,
            com_papel=com_papel,
            outra_empresa=outra_empresa,
            pedido=pedido,
            aprovar=aprovar,
            conectar=conectar,
        )

    return _nova


class _Conta:
    def __init__(self, **campos):
        self.__dict__.update(campos)


# --- descoberta ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caminho",
    ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"],
)
def test_metadados_do_recurso(mcp, caminho):
    resposta = client.get(caminho)

    assert resposta.status_code == 200
    assert resposta.json() | {"resource_name": None} == {
        "resource": RECURSO,
        "authorization_servers": [ISSUER],
        "scopes_supported": ["ledgr.leitura"],
        "bearer_methods_supported": ["header"],
        "resource_name": None,
    }


def test_metadados_do_servidor_de_autorizacao(mcp):
    corpo = client.get("/.well-known/oauth-authorization-server").json()

    assert corpo["issuer"] == ISSUER
    assert corpo["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert corpo["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert corpo["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert corpo["revocation_endpoint"] == f"{ISSUER}/oauth/revoke"
    assert corpo["response_types_supported"] == ["code"]
    assert corpo["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert corpo["code_challenge_methods_supported"] == ["S256"]
    assert corpo["token_endpoint_auth_methods_supported"] == ["none"]
    assert corpo["scopes_supported"] == ["ledgr.leitura", "offline_access"]
    assert corpo["authorization_response_iss_parameter_supported"] is True
    assert "client_id_metadata_document_supported" not in corpo


def test_barra_no_fim_das_urls_configuradas_nao_vaza(mcp, monkeypatch):
    monkeypatch.setattr(settings, "mcp_url_publica", RECURSO + "/")
    monkeypatch.setattr(settings, "oauth_issuer", ISSUER + "/")

    assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == RECURSO
    assert client.get("/.well-known/oauth-authorization-server").json()["issuer"] == ISSUER


# --- flag e configuração -------------------------------------------------------------------

ROTAS = [
    ("get", "/.well-known/oauth-protected-resource"),
    ("get", "/.well-known/oauth-protected-resource/mcp"),
    ("get", "/.well-known/oauth-authorization-server"),
    ("post", "/oauth/register"),
    ("get", "/oauth/authorize"),
    ("post", "/oauth/token"),
    ("post", "/oauth/revoke"),
    ("get", "/conexoes-mcp"),
    ("delete", f"/conexoes-mcp/{uuid.uuid4()}"),
    ("get", "/conexoes-mcp/pedidos/x"),
    ("post", "/conexoes-mcp/pedidos/x/aprovar"),
    ("post", "/conexoes-mcp/pedidos/x/recusar"),
]


@pytest.mark.parametrize(("metodo", "caminho"), ROTAS)
def test_flag_desligada_da_404_em_tudo(metodo, caminho):
    assert getattr(client, metodo)(caminho).status_code == 404


def test_flag_desligada_a_dependency_da_404():
    assert usar("ledgr_mcp_qualquer").status_code == 404


@pytest.mark.parametrize(("metodo", "caminho"), ROTAS)
def test_sem_urls_configuradas_da_503(monkeypatch, metodo, caminho):
    monkeypatch.setattr(settings, "mcp_habilitado", True)

    assert getattr(client, metodo)(caminho).status_code == 503
    assert usar("ledgr_mcp_qualquer").status_code == 503


# --- registro ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "redirect",
    [
        CALLBACK_CLAUDE,
        CALLBACK_CHATGPT,
        "https://chatgpt.com/connector/oauth/abc_123-XYZ",
        "http://localhost:3118/callback",
        "http://127.0.0.1:54321/callback",
        "http://localhost/callback",
    ],
)
def test_registro_aceita_os_redirects_da_lista(mcp, db_session, redirect):
    resposta = registrar(redirect)

    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    assert corpo["redirect_uris"] == [redirect]
    assert corpo["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in corpo
    salvo = db_session.get(ClienteOAuth, corpo["client_id"])
    assert salvo.client_name == "Claude" and salvo.redirect_uris == [redirect]


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/callback",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback?x=1",
        "https://chatgpt.com/connector/oauth/a/b",
        "https://chatgpt.com/connector/oauth/",
        "http://localhost.evil.example/callback",
        "https://localhost/callback",
        "http://192.168.0.10/callback",
        "nao-e-url",
    ],
)
def test_registro_recusa_redirect_fora_da_lista(mcp, redirect):
    resposta = registrar(redirect)

    assert resposta.status_code == 400
    assert resposta.json()["error"] == "invalid_redirect_uri"


def test_um_redirect_ruim_entre_bons_recusa_o_registro(mcp):
    resposta = registrar([CALLBACK_CLAUDE, "https://evil.example/cb"])

    assert resposta.status_code == 400 and resposta.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "extra",
    [
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["client_credentials"]},
        {"response_types": ["token"]},
        {"client_name": "x" * 101},
        {"scope": "admin"},
    ],
)
def test_registro_so_de_cliente_publico_com_code(mcp, extra):
    resposta = registrar(**extra)

    assert resposta.status_code == 400
    assert resposta.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("corpo", [b"nao e json", b"[]", b""])
def test_registro_com_corpo_invalido(mcp, corpo):
    resposta = client.post(
        "/oauth/register", content=corpo, headers={"Content-Type": "application/json"}
    )

    assert resposta.status_code == 400
    assert resposta.json()["error"] == "invalid_client_metadata"


def test_registro_sem_campos_opcionais_vira_cliente_publico(mcp):
    resposta = client.post("/oauth/register", json={"redirect_uris": [CALLBACK_CLAUDE]})

    assert resposta.status_code == 201
    assert resposta.json()["token_endpoint_auth_method"] == "none"


def test_registro_apaga_clientes_sem_conexao_com_mais_de_7_dias(mcp, db_session, conta):
    c = conta()
    antigo_sem, antigo_com = registrar().json()["client_id"], None
    antigo_com, _ = c.conectar()
    db_session.execute(
        text(
            "UPDATE clientes_oauth SET criado_em = now() - interval '8 days' "
            "WHERE client_id IN (:a, :b)"
        ),
        {"a": antigo_sem, "b": antigo_com},
    )
    db_session.commit()

    registrar()

    db_session.expire_all()
    assert db_session.get(ClienteOAuth, antigo_sem) is None
    assert db_session.get(ClienteOAuth, antigo_com) is not None


def test_rate_limit_do_registro_por_ip(mcp):
    for _ in range(10):
        assert registrar().status_code == 201
    assert registrar().status_code == 429


# --- authorize -----------------------------------------------------------------------------


def test_authorize_valido_grava_o_pedido_e_vai_para_o_front(mcp, db_session):
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()

    resposta = autorizar(client_id, challenge=challenge)

    assert resposta.status_code == 302
    pedido = _query(resposta.headers["location"])["pedido"]
    assert len(pedido) >= 43
    linha = db_session.execute(
        text("SELECT * FROM pedidos_autorizacao_oauth WHERE client_id = :c"), {"c": client_id}
    ).one()
    assert linha.hash == _hash(pedido) and pedido not in [str(v) for v in linha]
    assert linha.state == "estado-do-cliente" and linha.resource == RECURSO
    assert linha.scope == "ledgr.leitura offline_access"
    assert linha.code_challenge == challenge


def test_authorize_sem_escopo_vale_ledgr_leitura(mcp, db_session):
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()

    autorizar(client_id, challenge=challenge, scope=None)

    escopo = db_session.scalar(
        text("SELECT scope FROM pedidos_autorizacao_oauth WHERE client_id = :c"), {"c": client_id}
    )
    assert escopo == "ledgr.leitura"


@pytest.mark.parametrize(
    "params",
    [
        {"client_id": "nao-existe"},
        {"client_id": None},
        {"redirect_uri": "https://evil.example/callback"},
        {"redirect_uri": CALLBACK_CHATGPT},
        {"redirect_uri": None},
    ],
)
def test_authorize_com_cliente_ou_redirect_errado_mostra_pagina_e_nao_redireciona(mcp, params):
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()
    params = {"client_id": client_id} | params

    resposta = autorizar(
        redirect=params.pop("redirect_uri", CALLBACK_CLAUDE), challenge=challenge, **params
    )

    assert resposta.status_code == 400
    assert "location" not in resposta.headers
    assert resposta.headers["content-type"].startswith("text/html")


def test_authorize_loopback_ignora_a_porta(mcp):
    client_id = registrar("http://127.0.0.1:5000/callback").json()["client_id"]
    _, challenge = _pkce()

    resposta = autorizar(client_id, "http://127.0.0.1:61234/callback", challenge)

    assert resposta.status_code == 302
    assert "/autorizar-conexao?pedido=" in resposta.headers["location"]


@pytest.mark.parametrize(
    ("params", "erro"),
    [
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": "curto"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"scope": "ledgr.escrita"}, "invalid_scope"),
        ({"resource": "https://outro.example/mcp"}, "invalid_target"),
        ({"resource": None}, "invalid_target"),
    ],
)
def test_authorize_outros_erros_voltam_ao_cliente_com_state_e_iss(mcp, params, erro):
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()
    base = {"challenge": challenge}
    if "code_challenge" in params:
        base["challenge"] = params.pop("code_challenge")

    resposta = autorizar(client_id, CALLBACK_CLAUDE, base["challenge"], **params)

    assert resposta.status_code == 302
    destino = resposta.headers["location"]
    assert destino.startswith(CALLBACK_CLAUDE + "?")
    assert _query(destino) | {"error_description": None} == {
        "error": erro,
        "state": "estado-do-cliente",
        "iss": ISSUER,
        "error_description": None,
    }


def test_resource_com_host_em_maiusculas_e_barra_no_fim_vale(mcp):
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()

    resposta = autorizar(client_id, challenge=challenge, resource="HTTPS://API.LEDGR.TESTE/mcp/")

    assert "/autorizar-conexao?pedido=" in resposta.headers["location"]


def test_authorize_sem_frontend_url_da_503(mcp, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "")
    client_id = registrar().json()["client_id"]
    _, challenge = _pkce()

    assert autorizar(client_id, challenge=challenge).status_code == 503


# --- consentimento -------------------------------------------------------------------------


def test_pedido_mostra_cliente_origem_escopo_e_empresas(mcp, conta):
    c = conta()
    segunda = c.outra_empresa()
    _, _, pedido = c.pedido()

    resposta = client.get(f"/conexoes-mcp/pedidos/{pedido}", headers=c.cabecalho())

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["cliente"] == "Claude"
    assert corpo["origem"] == {"host": "claude.ai", "loopback": False}
    assert corpo["escopo"] == "ledgr.leitura" and corpo["descricao_escopo"]
    assert {e["id"] for e in corpo["empresas"]} == {str(c.empresa_id), str(segunda)}


def test_pedido_de_loopback_avisa(mcp, conta):
    c = conta()
    _, _, pedido = c.pedido(redirect="http://localhost:3118/callback", nome="Claude Code")

    corpo = client.get(f"/conexoes-mcp/pedidos/{pedido}", headers=c.cabecalho()).json()

    assert corpo["origem"] == {"host": "localhost", "loopback": True}


def test_aprovacao_redireciona_com_code_state_e_iss(mcp, conta, db_session):
    c = conta()
    _, _, pedido = c.pedido()

    resposta = c.aprovar(pedido)

    assert resposta.status_code == 200
    destino = resposta.json()["redirecionar_para"]
    assert destino.startswith(CALLBACK_CLAUDE + "?")
    params = _query(destino)
    assert set(params) == {"code", "state", "iss"}
    assert params["state"] == "estado-do-cliente" and params["iss"] == ISSUER
    codigo = db_session.scalar(select(CodigoOAuth).where(CodigoOAuth.hash == _hash(params["code"])))
    assert codigo is not None and codigo.resource == RECURSO
    conexao = db_session.get(ConexaoMcp, codigo.conexao_id)
    assert conexao.conta_id == c.admin.conta_id and conexao.empresa_id == c.empresa_id
    assert conexao.criada_por_id == c.admin.id and conexao.nome_cliente == "Claude"


def test_gestor_aprova_e_analista_recebe_403(mcp, conta):
    c = conta()
    _, _, pedido = c.pedido()

    analista = c.aprovar(pedido, usuario=c.com_papel("analista"))
    gestor = c.aprovar(pedido, usuario=c.com_papel("gestor"))

    assert analista.status_code == 403
    assert analista.json() == {"detail": "Só o admin ou o gestor da conta podem fazer isso."}
    assert gestor.status_code == 200


def test_empresa_de_outra_conta_da_404(mcp, conta):
    c, outra = conta(), conta()
    _, _, pedido = c.pedido()

    resposta = c.aprovar(pedido, empresa=outra.empresa_id)

    assert resposta.status_code == 404 and resposta.json() == {"detail": "Empresa não encontrada."}
    assert c.aprovar(pedido).status_code == 200  # o pedido continua valendo


def test_recusar_volta_com_access_denied(mcp, conta):
    c = conta()
    _, _, pedido = c.pedido()

    resposta = client.post(
        f"/conexoes-mcp/pedidos/{pedido}/recusar", headers=c.cabecalho(c.com_papel("analista"))
    )

    assert resposta.status_code == 200
    assert _query(resposta.json()["redirecionar_para"]) == {
        "error": "access_denied",
        "state": "estado-do-cliente",
        "iss": ISSUER,
    }
    assert c.aprovar(pedido).status_code == 404


def test_pedido_usado_ou_expirado_da_404(mcp, conta, db_session):
    c = conta()
    _, _, usado = c.pedido()
    c.aprovar(usado)
    _, _, expirado = c.pedido()
    db_session.execute(
        text(
            "UPDATE pedidos_autorizacao_oauth SET expira_em = now() - interval '1 second' WHERE hash = :h"
        ),
        {"h": _hash(expirado)},
    )
    db_session.commit()

    for pedido in (usado, expirado, "inexistente"):
        assert (
            client.get(f"/conexoes-mcp/pedidos/{pedido}", headers=c.cabecalho()).status_code == 404
        )
        assert c.aprovar(pedido).status_code == 404
        assert (
            client.post(
                f"/conexoes-mcp/pedidos/{pedido}/recusar", headers=c.cabecalho()
            ).status_code
            == 404
        )


def test_limite_de_10_conexoes_ativas(mcp, conta, db_session):
    c = conta()
    cliente = registrar().json()["client_id"]
    for _ in range(10):
        db_session.add(
            ConexaoMcp(
                conta_id=c.admin.conta_id,
                empresa_id=c.empresa_id,
                client_id=cliente,
                nome_cliente="Claude",
                escopo="ledgr.leitura",
                criada_por_id=c.admin.id,
            )
        )
    db_session.commit()
    _, _, pedido = c.pedido()

    resposta = c.aprovar(pedido)

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": "Sua conta já tem 10 conexões ativas. Revogue uma antes de criar outra."
    }
    uma = db_session.scalar(select(ConexaoMcp.id).where(ConexaoMcp.conta_id == c.admin.conta_id))
    client.delete(f"/conexoes-mcp/{uma}", headers=c.cabecalho())
    assert c.aprovar(pedido).status_code == 200


# --- token ---------------------------------------------------------------------------------


def test_fluxo_completo(mcp, conta, db_session):
    c = conta()
    client_id, verifier, pedido = c.pedido()
    code = _query(c.aprovar(pedido).json()["redirecionar_para"])["code"]

    resposta = trocar_codigo(client_id, code, verifier)

    assert resposta.status_code == 200, resposta.text
    assert resposta.headers["cache-control"] == "no-store"
    tokens = resposta.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    assert tokens["access_token"].startswith("ledgr_mcp_")
    assert tokens["refresh_token"] and tokens["scope"] == "ledgr.leitura offline_access"
    salvo = db_session.scalar(
        select(TokenOAuth).where(TokenOAuth.hash_acesso == _hash(tokens["access_token"]))
    )
    assert salvo.hash_renovacao == _hash(tokens["refresh_token"]) and salvo.resource == RECURSO
    valores = [
        str(v)
        for v in db_session.execute(
            text("SELECT * FROM tokens_oauth WHERE id = :i"), {"i": salvo.id}
        ).one()
    ]
    assert tokens["access_token"] not in valores and tokens["refresh_token"] not in valores

    uso = usar(tokens["access_token"])
    assert uso.status_code == 200
    assert uso.json()["empresa_id"] == str(c.empresa_id)
    assert uso.json()["conta_id"] == str(c.admin.conta_id)

    renovado = renovar(client_id, tokens["refresh_token"])
    assert renovado.status_code == 200, renovado.text
    novos = renovado.json()
    assert novos["access_token"] != tokens["access_token"]
    assert novos["refresh_token"] != tokens["refresh_token"]
    assert usar(novos["access_token"]).status_code == 200
    assert renovar(client_id, novos["refresh_token"]).status_code == 200


def test_renovacao_sai_mesmo_sem_offline_access(mcp, conta):
    c = conta()
    registro = registrar(CALLBACK_CHATGPT, "ChatGPT").json()["client_id"]
    verifier, challenge = _pkce()
    destino = autorizar(registro, CALLBACK_CHATGPT, challenge, scope="ledgr.leitura").headers[
        "location"
    ]
    code = _query(c.aprovar(_query(destino)["pedido"]).json()["redirecionar_para"])["code"]

    tokens = trocar_codigo(registro, code, verifier, CALLBACK_CHATGPT).json()

    assert tokens["scope"] == "ledgr.leitura" and tokens["refresh_token"]


@pytest.mark.parametrize(
    ("alterar", "erro"),
    [
        ({"code_verifier": "x" * 50}, "invalid_grant"),
        ({"code_verifier": None}, "invalid_request"),
        ({"redirect_uri": CALLBACK_CHATGPT}, "invalid_grant"),
        ({"resource": "https://outro.example/mcp"}, "invalid_target"),
        ({"code": "codigo-que-nao-existe"}, "invalid_grant"),
        ({"client_id": "outro-cliente"}, "invalid_client"),
    ],
)
def test_erros_na_troca_do_codigo(mcp, conta, alterar, erro):
    c = conta()
    client_id, verifier, pedido = c.pedido()
    code = _query(c.aprovar(pedido).json()["redirecionar_para"])["code"]
    params = {"client_id": client_id, "code": code, "code_verifier": verifier, **alterar}

    resposta = trocar_codigo(
        params.pop("client_id"), params.pop("code"), params.pop("code_verifier"), **params
    )

    assert resposta.status_code in (400, 401)
    assert resposta.json()["error"] == erro


def test_codigo_reutilizado_revoga_os_tokens_dele(mcp, conta):
    c = conta()
    client_id, verifier, pedido = c.pedido()
    code = _query(c.aprovar(pedido).json()["redirecionar_para"])["code"]
    primeiro = trocar_codigo(client_id, code, verifier).json()

    segundo = trocar_codigo(client_id, code, verifier)

    assert segundo.status_code == 400 and segundo.json()["error"] == "invalid_grant"
    assert usar(primeiro["access_token"]).status_code == 401
    assert renovar(client_id, primeiro["refresh_token"]).json()["error"] == "invalid_grant"


def test_codigo_expirado(mcp, conta, db_session):
    c = conta()
    client_id, verifier, pedido = c.pedido()
    code = _query(c.aprovar(pedido).json()["redirecionar_para"])["code"]
    db_session.execute(
        text("UPDATE codigos_oauth SET expira_em = now() - interval '1 second' WHERE hash = :h"),
        {"h": _hash(code)},
    )
    db_session.commit()

    resposta = trocar_codigo(client_id, code, verifier)

    assert resposta.status_code == 400 and resposta.json()["error"] == "invalid_grant"


def test_reuso_do_token_de_renovacao_revoga_a_conexao(mcp, conta, db_session):
    c = conta()
    client_id, tokens = c.conectar()
    novos = renovar(client_id, tokens["refresh_token"]).json()

    reuso = renovar(client_id, tokens["refresh_token"])

    assert reuso.status_code == 400 and reuso.json()["error"] == "invalid_grant"
    assert usar(novos["access_token"]).status_code == 401
    assert renovar(client_id, novos["refresh_token"]).json()["error"] == "invalid_grant"
    conexao = db_session.scalar(select(ConexaoMcp).where(ConexaoMcp.client_id == client_id))
    assert conexao.revogada_em is not None


def test_renovacao_com_outro_cliente_ou_resource_errado(mcp, conta):
    c = conta()
    client_id, tokens = c.conectar()
    outro = registrar().json()["client_id"]

    assert renovar(outro, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert (
        renovar(client_id, tokens["refresh_token"], resource="https://x.example").json()["error"]
        == "invalid_target"
    )
    assert renovar(client_id, tokens["refresh_token"]).status_code == 200


def test_renovacao_expirada(mcp, conta, db_session):
    c = conta()
    client_id, tokens = c.conectar()
    db_session.execute(
        text(
            "UPDATE tokens_oauth SET renovacao_expira_em = now() - interval '1 second' "
            "WHERE hash_renovacao = :h"
        ),
        {"h": _hash(tokens["refresh_token"])},
    )
    db_session.commit()

    assert renovar(client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_erros_do_token_no_formato_do_rfc_6749(mcp):
    sem_tipo = client.post("/oauth/token", data={"client_id": "x"})
    tipo_desconhecido = client.post("/oauth/token", data={"grant_type": "password"})

    assert sem_tipo.status_code == 400 and sem_tipo.json()["error"] == "unsupported_grant_type"
    assert tipo_desconhecido.json()["error"] == "unsupported_grant_type"


# --- revogação -----------------------------------------------------------------------------


def test_revogar_pelo_app_corta_acesso_e_renovacao_na_hora(mcp, conta, db_session):
    c = conta()
    client_id, tokens = c.conectar()
    conexao_id = db_session.scalar(select(ConexaoMcp.id).where(ConexaoMcp.client_id == client_id))

    primeira = client.delete(f"/conexoes-mcp/{conexao_id}", headers=c.cabecalho())
    segunda = client.delete(f"/conexoes-mcp/{conexao_id}", headers=c.cabecalho())

    assert primeira.status_code == 204 and segunda.status_code == 204
    assert usar(tokens["access_token"]).status_code == 401
    assert renovar(client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    db_session.expire_all()
    assert db_session.get(ConexaoMcp, conexao_id).revogada_por_id == c.admin.id


def test_revogar_conexao_de_outra_conta_da_404(mcp, conta, db_session):
    c, outra = conta(), conta()
    client_id, tokens = outra.conectar()
    conexao_id = db_session.scalar(select(ConexaoMcp.id).where(ConexaoMcp.client_id == client_id))

    assert client.delete(f"/conexoes-mcp/{conexao_id}", headers=c.cabecalho()).status_code == 404
    assert client.delete(f"/conexoes-mcp/{uuid.uuid4()}", headers=c.cabecalho()).status_code == 404
    assert usar(tokens["access_token"]).status_code == 200


def test_endpoint_de_revogacao(mcp, conta):
    c = conta()
    client_id, tokens = c.conectar()

    so_acesso = client.post(
        "/oauth/revoke", data={"token": tokens["access_token"], "client_id": client_id}
    )
    assert so_acesso.status_code == 200
    assert usar(tokens["access_token"]).status_code == 401
    novos = renovar(client_id, tokens["refresh_token"]).json()
    assert usar(novos["access_token"]).status_code == 200

    renovacao = client.post(
        "/oauth/revoke", data={"token": novos["refresh_token"], "client_id": client_id}
    )
    assert renovacao.status_code == 200
    assert usar(novos["access_token"]).status_code == 401
    desconhecido = client.post("/oauth/revoke", data={"token": "x", "client_id": client_id})
    assert desconhecido.status_code == 200


def test_remocao_do_criador_revoga(mcp, conta):
    c = conta()
    gestor = c.com_papel("gestor")
    _, tokens = c.conectar(usuario=gestor)
    _, do_admin = c.conectar()

    resposta = client.delete(f"/empresa/usuarios/{gestor.id}", headers=c.cabecalho())

    assert resposta.status_code == 204
    assert usar(tokens["access_token"]).status_code == 401
    assert usar(do_admin["access_token"]).status_code == 200


def test_rebaixamento_do_criador_a_analista_revoga(mcp, conta):
    c = conta()
    gestor = c.com_papel("gestor")
    _, tokens = c.conectar(usuario=gestor)

    resposta = client.patch(
        f"/empresa/usuarios/{gestor.id}", headers=c.cabecalho(), json={"papel": "analista"}
    )

    assert resposta.status_code == 200
    assert usar(tokens["access_token"]).status_code == 401


def test_transferencia_do_admin_nao_revoga(mcp, conta):
    from conftest import SENHA_USUARIO_TESTE

    c = conta()
    gestor = c.com_papel("gestor")
    _, tokens = c.conectar()

    resposta = client.post(
        "/empresa/admin/transferencia",
        headers=c.cabecalho(),
        json={"usuario_id": str(gestor.id), "senha_atual": SENHA_USUARIO_TESTE},
    )

    assert resposta.status_code == 200, resposta.text
    assert usar(tokens["access_token"]).status_code == 200


# --- lista ---------------------------------------------------------------------------------


def test_lista_de_conexoes(mcp, conta, db_session):
    c = conta()
    segunda = c.outra_empresa()
    primeiro, _ = c.conectar()
    c.conectar(empresa=segunda)
    revogada = db_session.scalar(select(ConexaoMcp.id).where(ConexaoMcp.client_id == primeiro))
    client.delete(f"/conexoes-mcp/{revogada}", headers=c.cabecalho())

    resposta = client.get("/conexoes-mcp", headers=c.cabecalho())

    assert resposta.status_code == 200
    itens = resposta.json()["itens"]
    assert [i["empresa"]["id"] for i in itens] == [str(segunda), str(c.empresa_id)]
    assert itens[0]["cliente"] == "Claude" and itens[0]["criada_por"] == "Ana Admin"
    assert itens[0]["empresa"]["razao_social"] == "Segunda Ltda"
    assert itens[0]["revogada_em"] is None and itens[1]["revogada_em"] is not None
    assert set(itens[0]) == {
        "id",
        "cliente",
        "empresa",
        "criada_por",
        "criado_em",
        "ultimo_uso_em",
        "revogada_em",
    }
    analista = client.get("/conexoes-mcp", headers=c.cabecalho(c.com_papel("analista")))
    assert analista.status_code == 403


# --- dependency do token -------------------------------------------------------------------


def test_conexao_de_uma_empresa_nao_le_outra(mcp, conta):
    c = conta()
    segunda = c.outra_empresa()

    _, tokens = c.conectar(empresa=segunda)

    assert usar(tokens["access_token"]).json()["empresa_id"] == str(segunda)


def test_401_tem_www_authenticate_com_resource_metadata_e_escopo(mcp):
    resposta = usar("ledgr_mcp_nao_existe")

    assert resposta.status_code == 401
    assert resposta.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource", '
        'scope="ledgr.leitura"'
    )


def test_jwt_do_app_e_chave_de_api_nao_valem_na_dependency(mcp, conta):
    c = conta()
    jwt_do_app = c.cabecalho()["Authorization"].removeprefix("Bearer ")
    chave = client.post("/chaves-api", headers=c.cabecalho(), json={"nome": "x"}).json()["chave"]

    for token in (jwt_do_app, chave, ""):
        resposta = client_mcp.get("/mcp-teste", headers={"Authorization": f"Bearer {token}"})
        assert resposta.status_code == 401
        assert "resource_metadata=" in resposta.headers["www-authenticate"]
    assert client_mcp.get("/mcp-teste").status_code == 401


def test_token_mcp_nao_vale_no_app_nem_na_v1(mcp, conta):
    c = conta()
    _, tokens = c.conectar()
    cabecalho = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert client.get("/me", headers=cabecalho).status_code == 401
    assert client.get("/conexoes-mcp", headers=cabecalho).status_code == 401
    assert client.get("/v1/empresas", headers=cabecalho).status_code == 401


def test_token_expirado_ou_de_outro_resource(mcp, conta, db_session, monkeypatch):
    c = conta()
    _, expirado = c.conectar()
    _, outro = c.conectar()
    db_session.execute(
        text(
            "UPDATE tokens_oauth SET acesso_expira_em = now() - interval '1 second' WHERE hash_acesso = :h"
        ),
        {"h": _hash(expirado["access_token"])},
    )
    db_session.commit()

    assert usar(expirado["access_token"]).status_code == 401
    assert usar(outro["access_token"]).status_code == 200
    monkeypatch.setattr(settings, "mcp_url_publica", "https://novo-dominio.teste/mcp")
    assert usar(outro["access_token"]).status_code == 401


def test_ultimo_uso_gravado_no_maximo_uma_vez_por_minuto(mcp, conta, db_session, monkeypatch):
    c = conta()
    client_id, tokens = c.conectar()
    chamadas = []
    original = auth.registrar_uso_da_conexao
    monkeypatch.setattr(
        auth, "registrar_uso_da_conexao", lambda i: (chamadas.append(i), original(i))
    )

    for _ in range(3):
        assert usar(tokens["access_token"]).status_code == 200

    conexao = db_session.scalar(select(ConexaoMcp).where(ConexaoMcp.client_id == client_id))
    assert len(chamadas) == 1 and chamadas[0] == conexao.id
    assert conexao.ultimo_uso_em is not None
    conexao.ultimo_uso_em = datetime.now(UTC) - timedelta(minutes=2)
    db_session.commit()
    usar(tokens["access_token"])
    assert len(chamadas) == 2


# --- logs ----------------------------------------------------------------------------------


def test_logs_sem_codigo_token_pedido_nem_state(mcp, conta, caplog):
    caplog.set_level(logging.INFO)
    c = conta()
    client_id, verifier, pedido = c.pedido()
    code = _query(c.aprovar(pedido).json()["redirecionar_para"])["code"]
    tokens = trocar_codigo(client_id, code, verifier).json()
    renovar(client_id, tokens["refresh_token"])

    # Só os logs do app: o cliente de teste (httpx) loga a URL que chamou.
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "oauth_token grant=authorization_code resultado=emitido" in texto
    for segredo in (
        code,
        pedido,
        verifier,
        tokens["access_token"],
        tokens["refresh_token"],
        "estado-do-cliente",
    ):
        assert segredo not in texto
