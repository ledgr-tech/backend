"""Testes de GET e PUT /empresa/configuracoes (issue #85): tolerância de dias
editável por empresa, de 0 a 5 (decisão de 05/10), e a integração com o motor.

Precisam de Postgres real (`db_session` do conftest.py) e pulam sem ele.
"""

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.models import Configuracao, Lancamento
from main import app

client = TestClient(app)

URL = "/empresa/configuracoes"


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _configuracao(db_session, empresa_id):
    db_session.expire_all()
    return db_session.query(Configuracao).filter_by(empresa_id=empresa_id).one_or_none()


VAZIO = {
    "tolerancia_dias": 0,
    "tolerancia_dias_maximo": 5,
    "atualizado_por": None,
    "atualizado_em": None,
}


def test_get_de_empresa_com_configuracao_padrao(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    db_session.add(Configuracao(empresa_id=usuario.empresa_id))
    db_session.commit()

    resposta = client.get(URL, headers=cabecalho_de(usuario))

    assert resposta.status_code == 200
    assert resposta.json() == VAZIO


def test_get_de_empresa_sem_linha_devolve_0_e_nao_cria(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = client.get(URL, headers=cabecalho_de(usuario))

    assert resposta.json() == VAZIO
    assert _configuracao(db_session, usuario.empresa_id) is None


@pytest.mark.parametrize("valor", [0, 1, 5])
def test_put_grava_e_o_get_seguinte_devolve(db_session, criar_usuario, cabecalho_de, valor):
    usuario = criar_usuario()
    db_session.add(Configuracao(empresa_id=usuario.empresa_id, tolerancia_dias_default=3))
    db_session.commit()

    put = client.put(URL, headers=cabecalho_de(usuario), json={"tolerancia_dias": valor})
    get = client.get(URL, headers=cabecalho_de(usuario))

    assert put.status_code == 200
    assert put.json() == get.json()
    corpo = get.json()
    assert corpo["tolerancia_dias"] == valor
    assert corpo["tolerancia_dias_maximo"] == 5
    assert corpo["atualizado_por"] == usuario.nome
    assert datetime.fromisoformat(corpo["atualizado_em"]).tzinfo is not None
    configuracao = _configuracao(db_session, usuario.empresa_id)
    assert configuracao.tolerancia_dias_default == valor
    assert configuracao.atualizado_por_id == usuario.id


@pytest.mark.parametrize(
    ("corpo", "loc"),
    [
        ({"tolerancia_dias": -1}, ["body", "tolerancia_dias"]),
        ({"tolerancia_dias": 6}, ["body", "tolerancia_dias"]),
        ({"tolerancia_dias": "3"}, ["body", "tolerancia_dias"]),
        ({"tolerancia_dias": 2.5}, ["body", "tolerancia_dias"]),
        ({"tolerancia_dias": True}, ["body", "tolerancia_dias"]),
        ({}, ["body", "tolerancia_dias"]),
        ({"tolerancia_dias": 1, "tolerancia_valor": "0.05"}, ["body", "tolerancia_valor"]),
        ({"tolerancia_dias": 1, "similaridade_minima": 80}, ["body", "similaridade_minima"]),
    ],
    ids=[
        "negativo",
        "acima_de_5",
        "string",
        "fracao",
        "booleano",
        "vazio",
        "valor",
        "similaridade",
    ],
)
def test_put_invalido_da_422_e_nao_altera(db_session, criar_usuario, cabecalho_de, corpo, loc):
    usuario = criar_usuario()
    db_session.add(Configuracao(empresa_id=usuario.empresa_id, tolerancia_dias_default=2))
    db_session.commit()

    resposta = client.put(URL, headers=cabecalho_de(usuario), json=corpo)

    assert resposta.status_code == 422
    assert loc in [erro["loc"] for erro in resposta.json()["detail"]]
    configuracao = _configuracao(db_session, usuario.empresa_id)
    assert configuracao.tolerancia_dias_default == 2
    assert configuracao.atualizado_por_id is None


def test_put_em_empresa_sem_linha_cria_a_linha(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = client.put(URL, headers=cabecalho_de(usuario), json={"tolerancia_dias": 4})

    assert resposta.status_code == 200
    assert resposta.json()["tolerancia_dias"] == 4
    assert _configuracao(db_session, usuario.empresa_id).tolerancia_dias_default == 4


def test_isolamento_entre_empresas(db_session, criar_usuario, cabecalho_de):
    a, b = criar_usuario(), criar_usuario()
    db_session.add(Configuracao(empresa_id=b.empresa_id, tolerancia_dias_default=2))
    db_session.commit()

    assert client.put(URL, headers=cabecalho_de(a), json={"tolerancia_dias": 5}).status_code == 200

    corpo_b = client.get(URL, headers=cabecalho_de(b)).json()
    assert corpo_b["tolerancia_dias"] == 2 and corpo_b["atualizado_por"] is None
    assert client.get(URL, headers=cabecalho_de(a)).json()["tolerancia_dias"] == 5


def test_sem_token_da_401():
    assert client.get(URL).status_code == 401
    assert client.put(URL, json={"tolerancia_dias": 1}).status_code == 401


def test_put_com_sub_inexistente_da_401(db_session, criar_empresa, gerar_token):
    token = gerar_token(criar_empresa().id, usuario_id=uuid.uuid4())

    resposta = client.put(
        URL, headers={"Authorization": f"Bearer {token}"}, json={"tolerancia_dias": 1}
    )

    assert resposta.status_code == 401


def _lancamento(db_session, extrato, dia):
    db_session.add(
        Lancamento(
            empresa_id=extrato.empresa_id,
            extrato_id=extrato.id,
            data=date(2026, 9, dia),
            valor=Decimal("10.00"),
            descricao="pagamento",
            tipo="credito",
            hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            ocorrencia=1,
        )
    )
    db_session.commit()


def test_motor_usa_a_tolerancia_alterada_pela_api(
    db_session, criar_usuario, criar_extrato_da_empresa, cabecalho_de
):
    usuario = criar_usuario()
    headers = cabecalho_de(usuario)
    banco = criar_extrato_da_empresa(usuario.empresa_id, "banco")
    sistema = criar_extrato_da_empresa(usuario.empresa_id, "sistema")
    _lancamento(db_session, banco, 4)
    _lancamento(db_session, sistema, 5)

    def conciliar():
        resposta = client.post(
            "/conciliacoes",
            headers=headers,
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )
        assert resposta.status_code == 201
        return resposta.json()

    def execucao_atual():
        return client.get(
            "/execucoes", headers=headers, params={"extrato_banco_id": str(banco.id)}
        ).json()["itens"][0]

    assert client.put(URL, headers=headers, json={"tolerancia_dias": 1}).status_code == 200
    assert conciliar()["match_tolerancia"] == 1
    assert execucao_atual()["tolerancia_dias"] == 1

    assert client.put(URL, headers=headers, json={"tolerancia_dias": 0}).status_code == 200
    assert conciliar()["match_tolerancia"] == 0
    execucao = execucao_atual()
    assert execucao["tolerancia_dias"] == 0
    assert execucao["contagens"]["match_tolerancia"] == 0
