"""Revogação de sessão por `auth_time` (issue #75, ADR-016).

O front reassina o JWT a cada renovação da sessão, com `iat` e `exp` novos.
Por isso o backend compara o `auth_time` (o momento do login, mantido nas
renovações) com o teto de 7 dias e com `usuarios.sessoes_validas_desde`,
gravado na troca de senha, na redefinição pelo link e na remoção do usuário.
Toda rota autenticada carrega o usuário do `sub`, inclusive as que só usam o
`empresa_id`.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DURACAO_SESSAO_SEGUNDOS
from app.models import Usuario
from main import app

client = TestClient(app)

# Uma rota que só lê o `empresa_id` e uma que carrega o usuário.
ROTAS_DE_LEITURA = ["/extratos", "/me"]


@pytest.fixture
def token_de(gerar_token):
    def _token(usuario: Usuario, **kwargs) -> dict[str, str]:
        token = gerar_token(usuario.empresa_id, usuario_id=usuario.id, **kwargs)
        return {"Authorization": f"Bearer {token}"}

    return _token


def _login_ha(segundos: int) -> int:
    return int(time.time()) - segundos


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
