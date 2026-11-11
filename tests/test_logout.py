"""`POST /logout` (issue #197, adendo de 11/11/2026 do ADR-016).

"Sair" encerra a sessão no backend: grava `usuarios.sessoes_validas_desde`, e
todo token da pessoa com `auth_time` anterior deixa de valer, em todos os
aparelhos. Sem `jti` e sem lista de revogados. Chave de API e conexão MCP não
são sessão do app e continuam valendo.
"""

# ruff: noqa: F811 - as fixtures importadas de outros testes são parâmetros aqui

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import SENHA_USUARIO_TESTE, cabecalhos
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import func, update
from test_mcp_servidor import (
    _chamar,
    cenario,  # noqa: F401 - fixture
    mcp,  # noqa: F401 - fixture usada com usefixtures
)
from test_revogacao_sessao import (
    _login_ha,
    _recarregar,
    token_de,  # noqa: F401 - fixture
)

from app.core import conta_inativa
from app.core.auth import DURACAO_SESSAO_SEGUNDOS
from app.models import Conta
from main import app

client = TestClient(app)


def _sair(cabecalho: dict[str, str] | None = None):
    return client.post("/logout", headers=cabecalho or {})


# --- o logout -------------------------------------------------------------------


def test_logout_responde_204_sem_corpo_e_encerra_a_sessao(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    assert usuario.sessoes_validas_desde is None
    cabecalho = token_de(usuario, auth_time=_login_ha(60))
    assert client.get("/me", headers=cabecalho).status_code == 200
    antes = int(time.time())

    resposta = _sair(cabecalho)

    assert resposta.status_code == 204
    assert resposta.content == b""
    desde = _recarregar(db_session, usuario).sessoes_validas_desde
    assert desde.microsecond == 0
    assert antes - 1 <= desde.timestamp() <= time.time()
    assert client.get("/me", headers=cabecalho).status_code == 401
    assert client.get("/extratos", headers=cabecalho).status_code == 401


def test_logout_derruba_o_outro_aparelho_e_a_renovacao(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    login_do_celular = _login_ha(3600)
    celular = token_de(usuario, auth_time=login_do_celular)
    assert client.get("/me", headers=celular).status_code == 200

    assert _sair(token_de(usuario, auth_time=_login_ha(60))).status_code == 204

    assert client.get("/me", headers=celular).status_code == 401
    # A renovação do NextAuth: `iat` e `exp` novos, `auth_time` do login antigo.
    renovado = token_de(usuario, auth_time=login_do_celular)
    assert client.get("/me", headers=renovado).status_code == 401


def test_login_depois_do_logout_vale_inclusive_no_mesmo_segundo(
    db_session,
    criar_usuario,
    token_de,
):
    usuario = criar_usuario()
    saida = datetime(2026, 11, 11, 15, 0, 0, 200_000, tzinfo=UTC)

    with freeze_time(saida) as relogio:
        antigo = token_de(usuario, auth_time=int(saida.timestamp()) - 60)
        assert _sair(antigo).status_code == 204

        relogio.move_to(saida + timedelta(milliseconds=700))  # ainda no mesmo segundo
        login = client.post("/login", json={"email": usuario.email, "senha": SENHA_USUARIO_TESTE})
        assert login.status_code == 200
        auth_time = login.json()["auth_time"]

        desde = _recarregar(db_session, usuario).sessoes_validas_desde
        assert auth_time == int(desde.timestamp()) == int(saida.timestamp())
        assert client.get("/me", headers=token_de(usuario, auth_time=auth_time)).status_code == 200
        assert client.get("/me", headers=antigo).status_code == 401


def test_repetir_o_logout_com_o_mesmo_token_e_401(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    cabecalho = token_de(usuario, auth_time=_login_ha(60))
    assert _sair(cabecalho).status_code == 204
    desde = _recarregar(db_session, usuario).sessoes_validas_desde

    repetido = _sair(cabecalho)

    assert repetido.status_code == 401
    assert repetido.json() == {"detail": "Não autenticado."}
    assert _recarregar(db_session, usuario).sessoes_validas_desde == desde


def test_logout_nao_loga_email_nem_token(db_session, criar_usuario, token_de, caplog):
    usuario = criar_usuario()
    cabecalho = token_de(usuario)

    with caplog.at_level(logging.INFO, logger="app.api.auth"):
        assert _sair(cabecalho).status_code == 204

    assert f"logout usuario_id={usuario.id}" in caplog.text
    assert usuario.email not in caplog.text
    assert cabecalho["Authorization"].removeprefix("Bearer ") not in caplog.text


# --- token recusado -------------------------------------------------------------


def test_logout_sem_token_ou_com_token_malformado_e_401(db_session):
    for cabecalho in (None, {"Authorization": "Bearer nao-e-um-jwt"}, {"Authorization": "x"}):
        resposta = _sair(cabecalho)

        assert resposta.status_code == 401
        assert resposta.json() == {"detail": "Não autenticado."}


def test_logout_com_token_de_outro_segredo_e_401(db_session, criar_usuario, gerar_token):
    usuario = criar_usuario()
    forjado = gerar_token(usuario.conta_id, usuario_id=usuario.id, secret="outro-segredo-" * 4)

    assert _sair({"Authorization": f"Bearer {forjado}"}).status_code == 401
    assert _recarregar(db_session, usuario).sessoes_validas_desde is None


def test_logout_com_sessao_vencida_e_401_e_nao_grava(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    vencido = token_de(usuario, auth_time=_login_ha(DURACAO_SESSAO_SEGUNDOS + 60))

    assert _sair(vencido).status_code == 401
    assert _recarregar(db_session, usuario).sessoes_validas_desde is None


def test_logout_de_usuario_removido_e_401(db_session, criar_usuario, token_de):
    admin = criar_usuario()
    convidado = criar_usuario(conta_id=admin.conta_id)
    cabecalho = token_de(convidado, auth_time=_login_ha(60))
    remover = client.delete(f"/empresa/usuarios/{convidado.id}", headers=token_de(admin))
    assert remover.status_code == 204

    assert _sair(cabecalho).status_code == 401
    assert _sair(token_de(convidado)).status_code == 401


def test_logout_de_usuario_inexistente_e_401(db_session, criar_empresa, gerar_token):
    token = gerar_token(criar_empresa().conta_id, usuario_id=uuid.uuid4())

    assert _sair({"Authorization": f"Bearer {token}"}).status_code == 401


# --- conta inativa --------------------------------------------------------------


def test_logout_esta_entre_as_escritas_liberadas_da_conta_inativa():
    assert ("POST", "/logout") in conta_inativa.LIBERADAS


def test_conta_inativa_sai_e_a_sessao_e_encerrada(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    db_session.execute(
        update(Conta)
        .where(Conta.id == usuario.conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()
    cabecalho = token_de(usuario, auth_time=_login_ha(60))
    # A conta está mesmo inativa: outra escrita é barrada.
    barrada = client.post("/chaves-api", headers=cabecalho, json={"nome": "A"})
    assert (barrada.status_code, barrada.json()["codigo"]) == (403, "conta_inativa")

    resposta = _sair(cabecalho)

    assert resposta.status_code == 204
    assert _recarregar(db_session, usuario).sessoes_validas_desde is not None
    assert client.get("/me", headers=cabecalho).status_code == 401


# --- isolamento -----------------------------------------------------------------


def test_logout_nao_derruba_outro_usuario_da_conta_nem_de_outra_conta(
    db_session,
    criar_usuario,
    token_de,
):
    usuario = criar_usuario()
    colega = criar_usuario(conta_id=usuario.conta_id)
    de_outra_conta = criar_usuario()
    do_colega = token_de(colega, auth_time=_login_ha(60))
    da_outra_conta = token_de(de_outra_conta, auth_time=_login_ha(60))

    assert _sair(token_de(usuario, auth_time=_login_ha(60))).status_code == 204

    assert client.get("/me", headers=do_colega).status_code == 200
    assert client.get("/me", headers=da_outra_conta).status_code == 200
    assert _recarregar(db_session, colega).sessoes_validas_desde is None
    assert _recarregar(db_session, de_outra_conta).sessoes_validas_desde is None


# --- chave de API e conexão MCP -------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_chave_de_api_e_conexao_mcp_continuam_valendo_depois_do_logout(cenario, gerar_token):
    chave = cenario.chave_api()
    token_mcp = cenario.conectar()
    # Login de um minuto atrás: o `cenario.jwt` é do mesmo segundo do logout, e
    # o login do mesmo segundo continua valendo (ADR-016).
    admin = cenario.admin
    do_app = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id, auth_time=_login_ha(60)))

    assert cenario.http.post("/logout", headers=do_app).status_code == 204

    assert cenario.http.get("/me", headers=do_app).status_code == 401
    v1 = cenario.http.get("/v1/empresas", headers={"Authorization": f"Bearer {chave}"})
    assert v1.status_code == 200
    assert _chamar(token_mcp, "empresa_conectada").is_error is not True


# --- rate limit -----------------------------------------------------------------


def test_logout_limita_por_usuario(db_session, criar_usuario, token_de):
    usuario, outro = criar_usuario(), criar_usuario()

    # Um login novo por saída: o token da saída anterior já não vale.
    codigos = [_sair(token_de(usuario)).status_code for _ in range(31)]

    assert codigos[:30] == [204] * 30
    assert codigos[30] == 429
    # O limite é da pessoa: outra, do mesmo IP, sai normalmente.
    assert _sair(token_de(outro)).status_code == 204


def test_logout_com_token_recusado_nao_gasta_o_limite(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    for _ in range(35):
        assert _sair({"Authorization": "Bearer nao-e-um-jwt"}).status_code == 401

    assert _sair(token_de(usuario)).status_code == 204


# --- OpenAPI --------------------------------------------------------------------


def test_logout_aparece_na_openapi_com_204_401_e_429():
    operacao = app.openapi()["paths"]["/logout"]["post"]

    assert set(operacao["responses"]) == {"204", "401", "429"}
    assert "requestBody" not in operacao
    assert "todos os aparelhos" in operacao["summary"]
    assert operacao["security"]
