"""Revogação de sessão por `auth_time` (issue #75, ADR-016).

O front reassina o JWT a cada renovação da sessão, com `iat` e `exp` novos.
Por isso o backend compara o `auth_time` (o momento do login, mantido nas
renovações) com o teto de 7 dias e com `usuarios.sessoes_validas_desde`,
gravado na troca de senha, na redefinição pelo link e na remoção do usuário.
Toda rota autenticada carrega o usuário do `sub`, inclusive as que só usam o
`empresa_id`.
"""

import hashlib
import io
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import SENHA_USUARIO_TESTE
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import func

from app.core.auth import DURACAO_SESSAO_SEGUNDOS
from app.models import TokenEmail, Usuario
from app.models.token_email import FINALIDADE_RECUPERACAO_SENHA
from main import app

client = TestClient(app)

SENHA_NOVA = "senha-nova-456"
# Uma rota que só lê o `empresa_id` e uma que carrega o usuário.
ROTAS_DE_LEITURA = ["/extratos", "/me"]


@pytest.fixture
def token_de(gerar_token):
    def _token(usuario: Usuario, **kwargs) -> dict[str, str]:
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, **kwargs)
        return {"Authorization": f"Bearer {token}"}

    return _token


def _login_ha(segundos: int) -> int:
    return int(time.time()) - segundos


def _recarregar(db_session, usuario: Usuario) -> Usuario:
    db_session.expire_all()
    return db_session.get(Usuario, usuario.id)


# --- o claim auth_time ----------------------------------------------------------


# Calculados na hora do teste, não na coleta: a suíte leva minutos, e um
# "no futuro" calculado na coleta já teria passado.
AUTH_TIME_RECUSADO = {
    "ausente": lambda: None,
    "float": lambda: float(_login_ha(60)),
    "string": lambda: str(_login_ha(60)),
    "booleano": lambda: True,
    "no_futuro": lambda: _login_ha(-120),
    "mais_de_7_dias": lambda: _login_ha(DURACAO_SESSAO_SEGUNDOS + 60),
}


@pytest.mark.parametrize("rota", ROTAS_DE_LEITURA)
@pytest.mark.parametrize("caso", AUTH_TIME_RECUSADO)
def test_auth_time_ausente_invalido_futuro_ou_vencido_e_401(
    db_session, criar_usuario, token_de, rota, caso
):
    usuario = criar_usuario()
    auth_time = AUTH_TIME_RECUSADO[caso]()

    response = client.get(rota, headers=token_de(usuario, auth_time=auth_time))

    assert response.status_code == 401
    assert response.json() == {"detail": "Não autenticado."}


@pytest.mark.parametrize("rota", ROTAS_DE_LEITURA)
def test_login_dentro_dos_7_dias_passa(db_session, criar_usuario, token_de, rota):
    usuario = criar_usuario()

    response = client.get(
        rota, headers=token_de(usuario, auth_time=_login_ha(DURACAO_SESSAO_SEGUNDOS - 60))
    )

    assert response.status_code == 200


def test_teto_conta_do_login_e_nao_da_renovacao(db_session, criar_usuario, token_de):
    """Token renovado agora (`iat` e `exp` novos) de um login de 8 dias atrás."""
    usuario = criar_usuario()
    renovado = token_de(usuario, auth_time=_login_ha(8 * 24 * 60 * 60))

    assert client.get("/extratos", headers=renovado).status_code == 401


# --- usuário do token -----------------------------------------------------------


@pytest.mark.parametrize("rota", ROTAS_DE_LEITURA)
def test_sub_inexistente_e_401_tambem_nas_rotas_que_so_leem_a_empresa(
    db_session, criar_empresa, gerar_token, rota
):
    token = gerar_token(criar_empresa().id, usuario_id=uuid.uuid4())

    assert client.get(rota, headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("rota", ROTAS_DE_LEITURA)
def test_sub_de_usuario_de_outra_empresa_e_401(
    db_session, criar_empresa, criar_usuario, gerar_token, rota
):
    de_outra_empresa = criar_usuario()
    empresa = criar_empresa()
    token = gerar_token(empresa.id, usuario_id=de_outra_empresa.id)

    assert client.get(rota, headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --- troca de senha logado ------------------------------------------------------


def test_troca_de_senha_encerra_as_sessoes_abertas(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    login_antigo = _login_ha(60)
    antigo = token_de(usuario, auth_time=login_antigo)
    for rota in ROTAS_DE_LEITURA:
        assert client.get(rota, headers=antigo).status_code == 200

    trocar = client.post(
        "/me/senha",
        json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": SENHA_NOVA},
        headers=antigo,
    )

    assert trocar.status_code == 204
    for rota in ROTAS_DE_LEITURA:
        assert client.get(rota, headers=antigo).status_code == 401
        # A renovação do NextAuth: `iat` e `exp` novos, `auth_time` do login antigo.
        renovado = token_de(usuario, auth_time=login_antigo)
        assert client.get(rota, headers=renovado).status_code == 401
        # O login de depois da troca.
        assert client.get(rota, headers=token_de(usuario)).status_code == 200


def test_troca_de_senha_grava_o_instante_truncado_no_segundo(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    assert usuario.sessoes_validas_desde is None
    antes = int(time.time())

    client.post(
        "/me/senha",
        json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": SENHA_NOVA},
        headers=token_de(usuario, auth_time=_login_ha(60)),
    )

    desde = _recarregar(db_session, usuario).sessoes_validas_desde
    assert desde.microsecond == 0
    assert antes - 1 <= desde.timestamp() <= time.time()


def test_troca_recusada_nao_encerra_as_sessoes(db_session, criar_usuario, token_de):
    usuario = criar_usuario()
    cabecalho = token_de(usuario, auth_time=_login_ha(60))

    trocar = client.post(
        "/me/senha",
        json={"senha_atual": "senha-errada-123", "senha_nova": SENHA_NOVA},
        headers=cabecalho,
    )

    assert trocar.status_code == 400
    assert _recarregar(db_session, usuario).sessoes_validas_desde is None
    assert client.get("/extratos", headers=cabecalho).status_code == 200


def test_troca_de_um_usuario_nao_derruba_o_outro_da_mesma_empresa(
    db_session, criar_usuario, token_de
):
    usuario = criar_usuario()
    colega = criar_usuario(conta_id=usuario.conta_id)
    do_colega = token_de(colega, auth_time=_login_ha(60))

    client.post(
        "/me/senha",
        json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": SENHA_NOVA},
        headers=token_de(usuario, auth_time=_login_ha(60)),
    )

    assert client.get("/extratos", headers=do_colega).status_code == 200


def test_login_logo_depois_da_troca_no_mesmo_segundo_passa(db_session, criar_usuario, gerar_token):
    """O front refaz o login logo depois da troca (`trocarSenha` chama
    `entrar`). Com o `auth_time` devolvido pelo `/login`, a troca e o login
    usam o mesmo relógio, e o login no mesmo segundo da troca vale."""
    usuario = criar_usuario()
    troca = datetime(2026, 10, 13, 15, 0, 0, 200_000, tzinfo=UTC)

    def cabecalho(auth_time: int) -> dict[str, str]:
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, auth_time=auth_time)
        return {"Authorization": f"Bearer {token}"}

    with freeze_time(troca) as relogio:
        antigo = cabecalho(int(troca.timestamp()) - 60)
        trocar = client.post(
            "/me/senha",
            json={"senha_atual": SENHA_USUARIO_TESTE, "senha_nova": SENHA_NOVA},
            headers=antigo,
        )
        assert trocar.status_code == 204

        relogio.move_to(troca + timedelta(milliseconds=700))  # ainda no mesmo segundo
        login = client.post("/login", json={"email": usuario.email, "senha": SENHA_NOVA})
        assert login.status_code == 200
        auth_time = login.json()["auth_time"]

        desde = _recarregar(db_session, usuario).sessoes_validas_desde
        assert auth_time == int(desde.timestamp()) == int(troca.timestamp())
        for rota in ROTAS_DE_LEITURA:
            assert client.get(rota, headers=cabecalho(auth_time)).status_code == 200
            assert client.get(rota, headers=antigo).status_code == 401


# --- redefinição pelo link ------------------------------------------------------


def _link_de_recuperacao(db_session, usuario: Usuario) -> str:
    token = uuid.uuid4().hex
    db_session.add(
        TokenEmail(
            usuario_id=usuario.id,
            finalidade=FINALIDADE_RECUPERACAO_SENHA,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expira_em=func.now() + timedelta(minutes=30),
        )
    )
    db_session.commit()
    return token


def test_redefinicao_pelo_link_encerra_as_sessoes_abertas(db_session, criar_usuario, token_de):
    """O caso da sessão roubada: quem tem o e-mail redefine a senha e quem
    tinha só o token perde o acesso, mesmo renovando."""
    usuario = criar_usuario()
    login_antigo = _login_ha(60)
    roubado = token_de(usuario, auth_time=login_antigo)
    assert client.get("/extratos", headers=roubado).status_code == 200

    redefinir = client.post(
        "/senha/redefinir",
        json={"token": _link_de_recuperacao(db_session, usuario), "senha_nova": SENHA_NOVA},
    )

    assert redefinir.status_code == 204
    for rota in ROTAS_DE_LEITURA:
        assert client.get(rota, headers=roubado).status_code == 401
        renovado = token_de(usuario, auth_time=login_antigo)
        assert client.get(rota, headers=renovado).status_code == 401
        assert client.get(rota, headers=token_de(usuario)).status_code == 200


# --- usuário removido (issue #69) -----------------------------------------------


CSV = b"data,valor,descricao\n2026-09-05,1500.00,Deposito ok\n"


def test_removido_perde_o_acesso_nas_rotas_que_so_leem_a_empresa(
    db_session, criar_usuario, token_de
):
    admin = criar_usuario()
    convidado = criar_usuario(conta_id=admin.conta_id)
    cabecalho = token_de(convidado, auth_time=_login_ha(60))
    assert client.get("/extratos", headers=cabecalho).status_code == 200

    remover = client.delete(f"/empresa/usuarios/{convidado.id}", headers=token_de(admin))
    assert remover.status_code == 204

    assert client.get("/extratos", headers=cabecalho).status_code == 401
    upload = client.post(
        "/extratos/upload",
        headers=cabecalho,
        data={"origem": "banco"},
        files={"arquivo": ("extrato.csv", io.BytesIO(CSV), "text/csv")},
    )
    assert upload.status_code == 401
    conciliar = client.post(
        "/conciliacoes",
        headers=cabecalho,
        json={"extrato_banco_id": str(uuid.uuid4()), "extrato_sistema_id": str(uuid.uuid4())},
    )
    assert conciliar.status_code == 401
    # Nem um token emitido agora, com o `auth_time` de depois da remoção.
    assert client.get("/extratos", headers=token_de(convidado)).status_code == 401


def test_remocao_grava_o_encerramento_das_sessoes(db_session, criar_usuario, token_de):
    admin = criar_usuario()
    convidado = criar_usuario(conta_id=admin.conta_id)

    client.delete(f"/empresa/usuarios/{convidado.id}", headers=token_de(admin))

    removido = _recarregar(db_session, convidado)
    assert removido.removido_em is not None
    assert removido.sessoes_validas_desde is not None
    assert removido.sessoes_validas_desde.microsecond == 0
