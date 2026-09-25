"""Testes de POST /register e POST /login (issue #57)."""

import random
import re
import string
import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.core.cnpj import digito_verificador, normalizar
from app.models import Configuracao, Empresa, Usuario
from main import app

client = TestClient(app)

SENHA = "senha-forte-123"


CNPJ_VALIDO = "12345678000195"


@pytest.fixture
def cadastro(db_session, gerar_cnpj):
    """Monta payloads de cadastro únicos e, no fim, apaga só o que eles criaram."""
    emails: list[str] = []
    cnpjs: list[str] = []

    def _payload(**sobrescritas) -> dict:
        dados = {
            "nome": "Maria Financeiro",
            "email": f"{uuid.uuid4().hex[:12]}@teste.com",
            "senha": SENHA,
            "razao_social": "Empresa de teste",
            "cnpj": gerar_cnpj(),
            **sobrescritas,
        }
        emails.append(dados["email"].strip().lower())
        cnpjs.append(re.sub(r"[^0-9A-Za-z]", "", dados["cnpj"]).upper())
        return dados

    yield _payload

    db_session.rollback()
    empresa_ids = [e.id for e in db_session.query(Empresa.id).filter(Empresa.cnpj.in_(cnpjs)).all()]
    db_session.query(Usuario).filter(Usuario.email.in_(emails)).delete(synchronize_session=False)
    db_session.query(Usuario).filter(Usuario.empresa_id.in_(empresa_ids)).delete(
        synchronize_session=False
    )
    db_session.query(Configuracao).filter(Configuracao.empresa_id.in_(empresa_ids)).delete(
        synchronize_session=False
    )
    db_session.query(Empresa).filter(Empresa.id.in_(empresa_ids)).delete(synchronize_session=False)
    db_session.commit()


def _registrar(dados: dict):
    return client.post("/register", json=dados)


def _login(email: str, senha: str = SENHA):
    return client.post("/login", json={"email": email, "senha": senha})


# --- cadastro -------------------------------------------------------------


def test_cadastro_cria_empresa_configuracao_e_usuario(db_session, cadastro, gerar_cnpj):
    d = gerar_cnpj()
    dados = cadastro(cnpj=f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}")

    response = _registrar(dados)

    assert response.status_code == 201
    corpo = response.json()
    assert set(corpo) == {"id", "empresa_id", "nome", "email"}
    assert corpo["email"] == dados["email"]

    usuario = db_session.get(Usuario, uuid.UUID(corpo["id"]))
    assert str(usuario.empresa_id) == corpo["empresa_id"]
    assert usuario.empresa.cnpj == d
    assert usuario.empresa.configuracao is not None


def test_cadastro_aceita_cnpj_alfanumerico_e_grava_em_maiusculas(db_session, cadastro):
    base = "".join(random.choices(string.ascii_uppercase + string.digits, k=8)) + "0001"
    base += digito_verificador(base)
    cnpj = base + digito_verificador(base)

    response = _registrar(cadastro(cnpj=cnpj.lower()))

    assert response.status_code == 201
    empresa = db_session.get(Empresa, uuid.UUID(response.json()["empresa_id"]))
    assert empresa.cnpj == cnpj


def test_cadastro_guarda_senha_so_como_hash(db_session, cadastro):
    corpo = _registrar(cadastro()).json()

    usuario = db_session.get(Usuario, uuid.UUID(corpo["id"]))
    assert usuario.senha_hash != SENHA
    assert bcrypt.checkpw(SENHA.encode(), usuario.senha_hash.encode())
    assert SENHA not in str(corpo)


def test_cadastro_com_email_duplicado_retorna_409(cadastro):
    primeiro = cadastro()
    assert _registrar(primeiro).status_code == 201

    response = _registrar(cadastro(email=primeiro["email"].upper()))

    assert response.status_code == 409
    assert response.json()["detail"] == "E-mail já cadastrado."


def test_cadastro_com_cnpj_duplicado_retorna_409(cadastro):
    primeiro = cadastro()
    assert _registrar(primeiro).status_code == 201

    response = _registrar(cadastro(cnpj=primeiro["cnpj"]))

    assert response.status_code == 409
    assert response.json()["detail"] == "CNPJ já cadastrado."


@pytest.mark.parametrize("campo", ["nome", "email", "senha", "razao_social", "cnpj"])
def test_cadastro_sem_campo_obrigatorio_retorna_422(campo):
    dados = {
        "nome": "Maria",
        "email": "maria@teste.com",
        "senha": SENHA,
        "razao_social": "Empresa",
        "cnpj": CNPJ_VALIDO,
    }
    del dados[campo]

    response = client.post("/register", json=dados)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", campo]


@pytest.mark.parametrize(
    "sobrescrita",
    [
        {"email": "nao-e-email"},
        {"senha": "curta"},
        {"senha": "á" * 37},  # 74 bytes: passa do limite do bcrypt
        {"cnpj": "123"},
        {"cnpj": "12345678000190"},  # dígito verificador errado
        {"cnpj": "00000000000000"},  # passa na conta, mas não é CNPJ
        {"cnpj": "12ABC34501DE3A"},  # letra na posição do verificador
        {"nome": "   "},
    ],
)
def test_cadastro_com_campo_invalido_retorna_422(sobrescrita):
    dados = {
        "nome": "Maria",
        "email": "maria@teste.com",
        "senha": SENHA,
        "razao_social": "Empresa",
        "cnpj": CNPJ_VALIDO,
        **sobrescrita,
    }

    response = client.post("/register", json=dados)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", next(iter(sobrescrita))]


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("12.345.678/0001-95", "12345678000195"),
        ("11222333000181", "11222333000181"),
        ("12.ABC.345/01DE-35", "12ABC34501DE35"),  # exemplo oficial da Receita
        ("12.abc.345/01de-35", "12ABC34501DE35"),
        ("12345678000190", None),
        ("11111111111111", None),
        ("1234567800019", None),
        ("123456780001955", None),
        ("12ABC34501DE3A", None),
    ],
)
def test_normalizar_cnpj(entrada, esperado):
    assert normalizar(entrada) == esperado


# --- login ----------------------------------------------------------------


def test_login_com_credenciais_validas_devolve_usuario_e_empresa(cadastro):
    cadastrado = _registrar(cadastro()).json()

    response = _login(f"  {cadastrado['email'].upper()} ")

    assert response.status_code == 200
    assert response.json() == cadastrado


def test_login_com_senha_errada_retorna_401(cadastro):
    email = _registrar(cadastro()).json()["email"]

    response = _login(email, "senha-errada")

    assert response.status_code == 401
    assert response.json()["detail"] == "E-mail ou senha inválidos."


def test_login_com_email_inexistente_responde_igual_senha_errada(db_session):
    response = _login(f"{uuid.uuid4().hex}@teste.com")

    assert response.status_code == 401
    assert response.json()["detail"] == "E-mail ou senha inválidos."


def test_login_de_usuario_so_com_google_retorna_401(db_session, criar_empresa):
    empresa = criar_empresa()
    usuario = Usuario(
        empresa_id=empresa.id,
        nome="Só Google",
        email=f"{uuid.uuid4().hex[:12]}@teste.com",
        google_sub=uuid.uuid4().hex,
    )
    db_session.add(usuario)
    db_session.commit()
    try:
        assert _login(usuario.email).status_code == 401
    finally:
        db_session.delete(usuario)
        db_session.commit()


@pytest.mark.parametrize("campo", ["email", "senha"])
def test_login_sem_campo_obrigatorio_retorna_422(campo):
    dados = {"email": "maria@teste.com", "senha": SENHA}
    del dados[campo]

    assert client.post("/login", json=dados).status_code == 422


def test_login_acima_de_10_por_minuto_retorna_429_na_11a(db_session):
    email = f"{uuid.uuid4().hex}@teste.com"

    codigos = [_login(email).status_code for _ in range(11)]

    assert codigos[:10] == [401] * 10
    assert codigos[10] == 429
