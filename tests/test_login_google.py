"""`POST /login/google` e `POST /register/google` (issue #73, adendo de 13/10
do ADR-004), e o que vale para as três rotas do Google, com o
`POST /convites/aceitar/google` (o resto dele em tests/test_convites_google.py).

Nenhum teste chama o Google: a fixture `google` (tests/conftest.py) gera uma
chave RSA na hora e troca o cliente de chaves por um falso. O e-mail de
boas-vindas vai para o provedor falso (fixture `provedor`).
"""

import logging
import math
import time
import uuid
from datetime import UTC, datetime

import pytest
from conftest import apagar_contas_por_cnpj
from fastapi.testclient import TestClient

from app.core import auth
from app.core.config import settings
from app.core.database import engine
from app.core.plano import DURACAO_TESTE
from app.models import Configuracao, Conta, Usuario
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)


def _email_google() -> str:
    return f"{uuid.uuid4().hex[:12]}@gmail.com"


@pytest.fixture
def cadastro_google(db_session, gerar_cnpj, google):
    """Monta o corpo do `/register/google` com um e-mail e um CNPJ únicos e,
    no fim, apaga só o que esses cadastros criaram."""
    cnpjs: list[str] = []

    def _corpo(email: str | None = None, sub: str | None = None, **sobrescritas) -> dict:
        claims = {"sub": sub} if sub else {}
        corpo = {
            "id_token": google.id_token(email=email or _email_google(), **claims),
            "razao_social": "Empresa do Google Ltda",
            "cnpj": gerar_cnpj(),
            **sobrescritas,
        }
        cnpjs.append(corpo["cnpj"])
        return corpo

    yield _corpo

    db_session.rollback()
    apagar_contas_por_cnpj(db_session, cnpjs)
    db_session.commit()


@pytest.fixture
def usuario_google(db_session, criar_usuario):
    """Usuário ativo que entra pelo Google: sem senha, com `google_sub`."""
    return criar_usuario(com_senha=False)


def _login_google(id_token: str):
    return client.post("/login/google", json={"id_token": id_token})


def _cabecalho(gerar_token, resposta: dict) -> dict[str, str]:
    """O token que o NextAuth monta com a resposta: id, conta_id e auth_time."""
    token = gerar_token(
        resposta["conta_id"], usuario_id=resposta["id"], auth_time=resposta["auth_time"]
    )
    return {"Authorization": f"Bearer {token}"}


# --- GOOGLE_CLIENT_ID vazio e Google fora do ar ----------------------------------


def _corpos_das_rotas(id_token: str, gerar_cnpj) -> dict[str, dict]:
    return {
        "/login/google": {"id_token": id_token},
        "/register/google": {
            "id_token": id_token,
            "razao_social": "Empresa",
            "cnpj": gerar_cnpj(),
        },
        "/convites/aceitar/google": {"token": "qualquer", "id_token": id_token},
    }


ROTAS_GOOGLE = ["/login/google", "/register/google", "/convites/aceitar/google"]


@pytest.mark.parametrize("rota", ROTAS_GOOGLE)
def test_google_client_id_vazio_e_503_sem_chamar_o_google(
    db_session, google, gerar_cnpj, monkeypatch, rota
):
    monkeypatch.setattr(settings, "google_client_id", "")
    corpo = _corpos_das_rotas(google.id_token(), gerar_cnpj)[rota]

    response = client.post(rota, json=corpo)

    assert response.status_code == 503
    assert response.json() == {"detail": "Login pelo Google indisponível."}
    assert google.cliente.buscas == 0


@pytest.mark.parametrize("rota", ROTAS_GOOGLE)
def test_falha_ao_buscar_as_chaves_e_503(db_session, google, gerar_cnpj, rota):
    google.cliente.falha_de_conexao = True
    corpo = _corpos_das_rotas(google.id_token(), gerar_cnpj)[rota]

    response = client.post(rota, json=corpo)

    assert response.status_code == 503
    assert response.json() == {"detail": "Login pelo Google indisponível."}


@pytest.mark.parametrize("rota", ROTAS_GOOGLE)
def test_token_invalido_e_401_nas_tres_rotas(db_session, google, gerar_cnpj, rota):
    corpo = _corpos_das_rotas(google.id_token(email_verified=False), gerar_cnpj)[rota]

    response = client.post(rota, json=corpo)

    assert response.status_code == 401
    assert response.json() == {"detail": "Não foi possível confirmar a conta Google."}


@pytest.mark.parametrize("rota", ROTAS_GOOGLE)
def test_chaves_buscadas_sem_conexao_do_banco_em_uso(db_session, google, gerar_cnpj, rota):
    """A busca das chaves vem antes de qualquer consulta: nenhuma transação da
    rota fica aberta esperando o Google."""
    corpo = _corpos_das_rotas(google.id_token(), gerar_cnpj)[rota]
    db_session.commit()  # devolve a conexão do próprio teste ao pool
    em_uso_antes = engine.pool.checkedout()

    client.post(rota, json=corpo)

    assert google.cliente.conexoes_em_uso == [em_uso_antes]


@pytest.mark.parametrize("rota", ROTAS_GOOGLE)
def test_rate_limit_por_ip(db_session, google, gerar_cnpj, rota):
    limite = 10 if rota == "/login/google" else 5
    corpo = _corpos_das_rotas("token-invalido", gerar_cnpj)[rota]

    for _ in range(limite):
        assert client.post(rota, json=corpo).status_code != 429
    assert client.post(rota, json=corpo).status_code == 429


# --- login ----------------------------------------------------------------------


def test_login_google_devolve_o_usuario_com_auth_time(db_session, google, usuario_google):
    antes = time.time()

    response = _login_google(
        google.id_token(email=usuario_google.email, sub=usuario_google.google_sub)
    )

    depois = time.time()
    assert response.status_code == 200
    corpo = response.json()
    assert corpo == {
        "id": str(usuario_google.id),
        "conta_id": str(usuario_google.conta_id),
        "empresa_id": str(usuario_google.conta_id),
        "nome": usuario_google.nome,
        "email": usuario_google.email,
        "auth_time": corpo["auth_time"],
    }
    assert type(corpo["auth_time"]) is int
    assert math.floor(antes) <= corpo["auth_time"] <= depois


def test_login_google_acha_pelo_sub_mesmo_com_outro_email(db_session, google, usuario_google):
    """O e-mail da conta Google pode mudar; o `sub` não."""
    token = google.id_token(email=_email_google(), sub=usuario_google.google_sub)

    response = _login_google(token)

    assert response.status_code == 200
    assert response.json()["id"] == str(usuario_google.id)


def test_token_do_login_google_passa_no_get_me(db_session, google, gerar_token, usuario_google):
    login = _login_google(
        google.id_token(email=usuario_google.email, sub=usuario_google.google_sub)
    )

    me = client.get("/me", headers=_cabecalho(gerar_token, login.json()))

    assert me.status_code == 200
    assert me.json()["metodos_login"] == ["google"]


def test_login_google_sem_conta_e_404(db_session, google):
    response = _login_google(google.id_token(email=_email_google()))

    assert response.status_code == 404
    assert response.json() == {"detail": "Não há conta no Ledgr para esta conta Google."}


def test_login_google_com_email_de_conta_com_senha_e_409_sem_vincular(
    db_session, google, criar_usuario
):
    com_senha = criar_usuario()

    response = _login_google(google.id_token(email=com_senha.email.upper()))

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Já existe uma conta com este e-mail. Entre com e-mail e senha."
    }
    db_session.refresh(com_senha)
    assert com_senha.google_sub is None


def test_login_google_de_usuario_removido_nao_entra(db_session, google, criar_usuario):
    admin = criar_usuario()
    convidado = criar_usuario(com_senha=False, conta_id=admin.conta_id)
    email, sub = convidado.email, convidado.google_sub
    remover_usuario(db_session, convidado)
    db_session.commit()

    response = _login_google(google.id_token(email=email, sub=sub))

    # O `google_sub` foi apagado e o e-mail virou o marcador: nenhuma conta.
    assert response.status_code == 404


def test_login_google_sem_id_token_e_422(db_session):
    assert client.post("/login/google", json={}).status_code == 422
    assert client.post("/login/google", json={"id_token": ""}).status_code == 422


# --- cadastro -------------------------------------------------------------------


def test_cadastro_google_cria_empresa_e_administrador_sem_senha(
    db_session, cadastro_google, gerar_token
):
    email = _email_google()
    corpo = cadastro_google(email=email.upper(), sub="10987654321")
    antes = time.time()

    response = client.post("/register/google", json=corpo)

    assert response.status_code == 201
    resposta = response.json()
    assert set(resposta) == {"id", "conta_id", "empresa_id", "nome", "email", "auth_time"}
    assert resposta["empresa_id"] == resposta["conta_id"]
    assert resposta["email"] == email
    assert resposta["nome"] == "Maria Google"
    assert math.floor(antes) <= resposta["auth_time"] <= time.time()

    usuario = db_session.get(Usuario, uuid.UUID(resposta["id"]))
    assert usuario.senha_hash is None
    assert usuario.google_sub == "10987654321"
    assert usuario.papel == "admin"
    conta = db_session.get(Conta, uuid.UUID(resposta["conta_id"]))
    assert (conta.cnpj, conta.razao_social) == (corpo["cnpj"], "Empresa do Google Ltda")
    (empresa,) = conta.empresas
    assert (empresa.cnpj, empresa.razao_social) == (corpo["cnpj"], "Empresa do Google Ltda")
    assert db_session.query(Configuracao).filter_by(empresa_id=empresa.id).count() == 1

    me = client.get("/me", headers=_cabecalho(gerar_token, resposta))
    assert me.status_code == 200
    assert me.json()["metodos_login"] == ["google"]
    assert me.json()["administrador"] is True

    # Daí em diante, o login pelo Google acha a conta.
    assert _login_google(corpo["id_token"]).json()["id"] == resposta["id"]


def test_cadastro_google_comeca_o_teste_de_14_dias_sem_plano(
    db_session, cadastro_google, gerar_token, monkeypatch
):
    """Issues #166 e #167: o mesmo teste do `/register`, sem plano e sem cartão."""
    instante = datetime.now(UTC)
    monkeypatch.setattr(auth, "_agora", lambda: instante)

    resposta = client.post("/register/google", json=cadastro_google()).json()

    conta = db_session.get(Conta, uuid.UUID(resposta["conta_id"]))
    assert conta.plano is None
    assert conta.teste_termina_em == instante + DURACAO_TESTE
    token = gerar_token(
        conta.id, usuario_id=resposta["id"], auth_time=math.floor(instante.timestamp())
    )
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert (me["plano"], me["situacao"], me["assinatura"]) == (None, "teste", None)


def test_cadastro_google_usa_o_nome_do_corpo(db_session, cadastro_google):
    response = client.post("/register/google", json=cadastro_google(nome="  Maria Silva  "))

    assert response.status_code == 201
    assert response.json()["nome"] == "Maria Silva"


def test_cadastro_google_sem_nome_no_corpo_nem_no_token_e_422(db_session, cadastro_google, google):
    corpo = cadastro_google()
    corpo["id_token"] = google.id_token(nome=None)

    response = client.post("/register/google", json=corpo)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "nome"]


@pytest.mark.parametrize(
    "sobrescrita",
    [{"cnpj": "12345678000100"}, {"razao_social": "   "}, {"nome": "  "}, {"cnpj": None}],
    ids=["cnpj_invalido", "razao_social_vazia", "nome_vazio", "sem_cnpj"],
)
def test_cadastro_google_com_campo_invalido_e_422(db_session, cadastro_google, sobrescrita):
    corpo = {**cadastro_google(), **sobrescrita}
    corpo = {chave: valor for chave, valor in corpo.items() if valor is not None}

    response = client.post("/register/google", json=corpo)

    assert response.status_code == 422
    campo = next(iter(sobrescrita))
    assert response.json()["detail"][0]["loc"] == ["body", campo]


def test_cadastro_google_com_email_ja_cadastrado_e_409(db_session, cadastro_google, criar_usuario):
    com_senha = criar_usuario()

    response = client.post("/register/google", json=cadastro_google(email=com_senha.email))

    assert response.status_code == 409
    assert response.json() == {"detail": "E-mail já cadastrado."}


def test_cadastro_google_com_cnpj_ja_cadastrado_e_409(db_session, cadastro_google, criar_empresa):
    empresa = criar_empresa()

    response = client.post("/register/google", json=cadastro_google(cnpj=empresa.cnpj))

    assert response.status_code == 409
    assert response.json() == {"detail": "CNPJ já cadastrado."}


def test_cadastro_google_com_google_sub_ja_cadastrado_e_409(
    db_session, cadastro_google, usuario_google
):
    corpo = cadastro_google(email=_email_google(), sub=usuario_google.google_sub)

    response = client.post("/register/google", json=corpo)

    assert response.status_code == 409
    assert response.json() == {"detail": "Já existe uma conta no Ledgr para esta conta Google."}


def test_cadastro_google_manda_boas_vindas(db_session, cadastro_google, provedor):
    email = _email_google()

    response = client.post("/register/google", json=cadastro_google(email=email))

    assert response.status_code == 201
    (mensagem,) = provedor.enviadas
    assert mensagem.para == email
    assert mensagem.chave_idempotencia == f"boas-vindas-{response.json()['id']}"


def test_conta_do_google_nao_entra_pelo_login_com_senha(db_session, cadastro_google):
    email = _email_google()
    assert client.post("/register/google", json=cadastro_google(email=email)).status_code == 201

    response = client.post("/login", json={"email": email, "senha": "qualquer-senha-1"})

    assert response.status_code == 401


# --- logs -----------------------------------------------------------------------


def test_logs_nao_tem_id_token_email_nem_sub(
    db_session, google, cadastro_google, usuario_google, criar_usuario, caplog
):
    caplog.set_level(logging.DEBUG)
    com_senha = criar_usuario()
    corpo = cadastro_google(sub="55555555555555555555")
    sem_conta = google.id_token(email="ninguem.aqui@gmail.com", sub="66666666666666666666")
    do_usuario = google.id_token(email=usuario_google.email, sub=usuario_google.google_sub)
    invalido = google.id_token(email="invalido.aqui@gmail.com", email_verified=False)
    tokens = [corpo["id_token"], sem_conta, do_usuario, invalido]

    client.post("/register/google", json=corpo)
    for token in [sem_conta, do_usuario, invalido, google.id_token(email=com_senha.email)]:
        _login_google(token)

    assert "login_google resultado=" in caplog.text
    assert "cadastro_google resultado=cadastrado" in caplog.text
    proibidos = [
        *tokens,
        "55555555555555555555",
        "66666666666666666666",
        usuario_google.google_sub,
        usuario_google.email,
        com_senha.email,
        "ninguem.aqui",
        "invalido.aqui",
        "@gmail.com",
    ]
    for texto in proibidos:
        assert texto not in caplog.text
