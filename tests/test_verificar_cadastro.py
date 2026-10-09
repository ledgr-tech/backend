"""Testes de POST /register/verificar (issue #80).

A rota diz se o e-mail e o CNPJ ainda estão livres, antes do fim do cadastro.
Não cria nada, valida igual ao /register e roda as duas consultas sempre, para
o tempo de resposta não depender de qual dos dois existe.
"""

import uuid

import pytest
from conftest import _gerar_cnpj
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.core.database import engine
from app.models import Empresa, Usuario
from main import app

client = TestClient(app)

CNPJ_VALIDO = "12345678000195"


def _verificar(email: str, cnpj: str):
    return client.post("/register/verificar", json={"email": email, "cnpj": cnpj})


def _email_livre() -> str:
    return f"{uuid.uuid4().hex[:12]}@teste.com"


def _cnpj_livre(db_session) -> str:
    while True:
        cnpj = _gerar_cnpj()
        if db_session.scalar(select(Empresa.id).where(Empresa.cnpj == cnpj)) is None:
            return cnpj


def _com_mascara(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


@pytest.fixture
def em_uso(db_session, criar_usuario):
    """Um usuário cadastrado: (e-mail, CNPJ da empresa dele)."""
    usuario = criar_usuario()
    return usuario.email, db_session.get(Empresa, usuario.empresa_id).cnpj


def test_email_e_cnpj_livres(db_session):
    response = _verificar(_email_livre(), _cnpj_livre(db_session))

    assert response.status_code == 200
    assert response.json() == {"email_disponivel": True, "cnpj_disponivel": True}


def test_so_o_email_em_uso(db_session, em_uso):
    email, _ = em_uso

    response = _verificar(email, _cnpj_livre(db_session))

    assert response.json() == {"email_disponivel": False, "cnpj_disponivel": True}


def test_so_o_cnpj_em_uso(db_session, em_uso):
    _, cnpj = em_uso

    response = _verificar(_email_livre(), cnpj)

    assert response.json() == {"email_disponivel": True, "cnpj_disponivel": False}


def test_email_e_cnpj_em_uso(em_uso):
    response = _verificar(*em_uso)

    assert response.json() == {"email_disponivel": False, "cnpj_disponivel": False}


def test_email_com_maiusculas_e_espacos_e_o_mesmo_cadastrado(db_session, em_uso):
    email, _ = em_uso

    response = _verificar(f"  {email.upper()} ", _cnpj_livre(db_session))

    assert response.json()["email_disponivel"] is False


def test_cnpj_com_e_sem_mascara_da_o_mesmo_resultado(db_session, em_uso):
    _, cnpj = em_uso
    livre = _cnpj_livre(db_session)

    for consultado, disponivel in ((cnpj, False), (livre, True)):
        sem_mascara = _verificar(_email_livre(), consultado).json()
        com_mascara = _verificar(_email_livre(), _com_mascara(consultado)).json()
        assert sem_mascara == com_mascara
        assert com_mascara["cnpj_disponivel"] is disponivel


@pytest.mark.parametrize(
    "sobrescrita",
    [
        {"email": "nao-e-email"},
        {"cnpj": "123"},
        {"cnpj": "12345678000190"},  # dígito verificador errado
        {"cnpj": "00000000000000"},  # passa na conta, mas não é CNPJ
    ],
)
def test_email_ou_cnpj_invalido_retorna_422_no_formato_do_register(sobrescrita):
    dados = {"email": "maria@teste.com", "cnpj": CNPJ_VALIDO, **sobrescrita}
    cadastro = {"nome": "Maria", "senha": "senha-forte-123", "razao_social": "E", **dados}

    response = client.post("/register/verificar", json=dados)
    do_register = client.post("/register", json=cadastro)

    assert response.status_code == 422
    campo = next(iter(sobrescrita))
    assert response.json()["detail"][0]["loc"] == ["body", campo]
    assert response.json() == do_register.json()


@pytest.mark.parametrize("campo", ["email", "cnpj"])
def test_campo_faltando_retorna_422(campo):
    dados = {"email": "maria@teste.com", "cnpj": CNPJ_VALIDO}
    del dados[campo]

    response = client.post("/register/verificar", json=dados)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", campo]


def test_a_11a_chamada_no_minuto_recebe_429(db_session):
    codigos = [_verificar(_email_livre(), CNPJ_VALIDO).status_code for _ in range(11)]

    assert codigos[:10] == [200] * 10
    assert codigos[10] == 429


def test_nao_cria_nenhuma_linha(db_session, em_uso):
    def _contagem():
        return (
            db_session.scalar(select(func.count()).select_from(Usuario)),
            db_session.scalar(select(func.count()).select_from(Empresa)),
        )

    antes = _contagem()
    _verificar(*em_uso)
    _verificar(_email_livre(), _cnpj_livre(db_session))
    db_session.rollback()  # enxergar o que outra sessão tenha gravado

    assert _contagem() == antes


@pytest.mark.parametrize(
    ("email_em_uso", "cnpj_em_uso"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_as_duas_consultas_rodam_sempre(db_session, em_uso, email_em_uso, cnpj_em_uso):
    email = em_uso[0] if email_em_uso else _email_livre()
    cnpj = em_uso[1] if cnpj_em_uso else _cnpj_livre(db_session)
    consultas: list[str] = []

    def _anotar(conn, cursor, statement, parameters, context, executemany):
        consultas.append(statement)

    event.listen(engine, "before_cursor_execute", _anotar)
    try:
        response = _verificar(email, cnpj)
    finally:
        event.remove(engine, "before_cursor_execute", _anotar)

    assert response.json() == {
        "email_disponivel": not email_em_uso,
        "cnpj_disponivel": not cnpj_em_uso,
    }
    assert len([c for c in consultas if "FROM usuarios" in c]) == 1
    assert len([c for c in consultas if "FROM empresas" in c]) == 1
