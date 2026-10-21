"""Limites de empresas e de usuários por plano (issue #133, parte 2, ADR-019).

Configuráveis por variável (`LIMITE_<RECURSO>_<PLANO>`) e vazios por padrão:
sem valor, não há limite. A conta sem plano usa os do essencial. No limite,
`POST /empresas` e `POST /empresa/convites` dão 402 para todos, inclusive o
admin. Quem já está acima continua com tudo, só não adiciona mais. Usuários
são os ativos mais os convites pendentes, a mesma conta dos `limites` do
`GET /me`.
"""

import re
import uuid
from datetime import timedelta

import pytest
from conftest import _gerar_cnpj, cabecalhos
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import auth
from app.core.auth import CABECALHO_EMPRESA
from app.core.config import Settings, settings
from app.core.plano import chegou_ao_limite, limite_da_conta
from app.models import Conta
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)

LIMITE_EMPRESAS = {"detail": "Sua conta chegou ao limite de empresas do plano."}
LIMITE_USUARIOS = {"detail": "Sua conta chegou ao limite de usuários do plano."}
RECURSOS_E_PLANOS = [
    (recurso, plano)
    for recurso in ("empresas", "usuarios")
    for plano in ("essencial", "padrao", "avancado")
]


@pytest.fixture
def limites(monkeypatch):
    """`limites(empresas_padrao=3, ...)` liga limites só neste teste."""

    def _ligar(**valores: int) -> None:
        for nome, valor in valores.items():
            monkeypatch.setattr(settings, f"limite_{nome}", valor)

    return _ligar


@pytest.fixture
def equipe(db_session, criar_usuario, gerar_token):
    admin = criar_usuario(nome="Ana Admin")
    gestor = criar_usuario(conta_id=admin.conta_id, papel="gestor")
    analista = criar_usuario(conta_id=admin.conta_id, papel="analista")
    headers = {
        nome: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id))
        for nome, u in {"admin": admin, "gestor": gestor, "analista": analista}.items()
    }
    return admin, headers


def _escolher_plano(db_session, conta_id, plano, assinatura_ativa=False):
    conta = db_session.get(Conta, conta_id)
    conta.plano = plano
    conta.teste_termina_em = auth._agora() + timedelta(days=14)
    conta.assinatura_ativa = assinatura_ativa
    db_session.commit()


def _cadastrar_empresa(headers):
    return client.post(
        "/empresas", headers=headers, json={"cnpj": _gerar_cnpj(), "razao_social": "Cliente X"}
    )


def _convidar(headers, email=None):
    return client.post(
        "/empresa/convites",
        headers=headers,
        json={"email": email or f"{uuid.uuid4().hex[:12]}@convidado.com"},
    )


def _limites_do_me(headers):
    return client.get("/me", headers=headers).json()["limites"]


# --- configuração e regra, sem banco ---------------------------------------------------


@pytest.mark.parametrize(("recurso", "plano"), RECURSOS_E_PLANOS)
def test_sem_variavel_nao_ha_limite(recurso, plano):
    configuracao = Settings(_env_file=None, database_url="postgresql://x/y")
    conta = Conta(plano=plano)

    assert limite_da_conta(conta, recurso, configuracao) is None
    assert chegou_ao_limite(10**6, None) is False


@pytest.mark.parametrize(("valor", "esperado"), [("", None), ("  ", None), ("7", 7)])
def test_variavel_vazia_e_sem_limite_e_numero_e_o_limite(monkeypatch, valor, esperado):
    monkeypatch.setenv("LIMITE_EMPRESAS_PADRAO", valor)

    assert Settings(_env_file=None, database_url="postgresql://x/y").limite_empresas_padrao == (
        esperado
    )


@pytest.mark.parametrize("valor", ["0", "-1", "dois"])
def test_limite_precisa_ser_inteiro_positivo(monkeypatch, valor):
    monkeypatch.setenv("LIMITE_USUARIOS_ESSENCIAL", valor)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x/y")


def test_o_limite_e_o_do_plano_escolhido_e_sem_plano_o_do_essencial():
    configuracao = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        limite_empresas_essencial=1,
        limite_empresas_padrao=5,
    )

    assert limite_da_conta(Conta(plano=None), "empresas", configuracao) == 1
    assert limite_da_conta(Conta(plano="essencial"), "empresas", configuracao) == 1
    assert limite_da_conta(Conta(plano="padrao"), "empresas", configuracao) == 5
    assert limite_da_conta(Conta(plano="avancado"), "empresas", configuracao) is None


@pytest.mark.parametrize(
    ("usados", "maximo", "esperado"),
    [(0, 1, False), (1, 1, True), (2, 1, True), (4, 5, False), (5, None, False)],
)
def test_chegou_ao_limite(usados, maximo, esperado):
    assert chegou_ao_limite(usados, maximo) is esperado


# --- empresas -------------------------------------------------------------------------


def test_sem_limite_configurado_a_conta_adiciona_empresas(db_session, equipe):
    _, headers = equipe

    assert [_cadastrar_empresa(headers["gestor"]).status_code for _ in range(3)] == [201] * 3
    assert _limites_do_me(headers["analista"])["empresas"] == {"usadas": 4, "maximo": None}


@pytest.mark.parametrize("quem", ["admin", "gestor"])
def test_no_limite_de_empresas_e_402_para_admin_e_gestor(db_session, equipe, limites, quem):
    _, headers = equipe
    limites(empresas_essencial=1)  # a conta sem plano usa o do essencial

    resposta = _cadastrar_empresa(headers[quem])

    assert resposta.status_code == 402
    assert resposta.json() == LIMITE_EMPRESAS
    assert _limites_do_me(headers[quem])["empresas"] == {"usadas": 1, "maximo": 1}


def test_o_analista_continua_recebendo_403_e_nao_402(db_session, equipe, limites):
    _, headers = equipe
    limites(empresas_essencial=1)

    assert _cadastrar_empresa(headers["analista"]).status_code == 403


def test_o_plano_escolhido_define_o_limite(db_session, equipe, limites):
    admin, headers = equipe
    limites(empresas_essencial=1, empresas_padrao=2)
    _escolher_plano(db_session, admin.conta_id, "padrao")

    assert _cadastrar_empresa(headers["admin"]).status_code == 201
    assert _cadastrar_empresa(headers["admin"]).status_code == 402


def test_assinatura_ativa_usa_o_plano_escolhido(db_session, equipe, limites):
    admin, headers = equipe
    limites(empresas_essencial=1, empresas_avancado=3)
    _escolher_plano(db_session, admin.conta_id, "avancado", assinatura_ativa=True)

    assert _cadastrar_empresa(headers["gestor"]).status_code == 201
    assert _limites_do_me(headers["gestor"])["empresas"] == {"usadas": 2, "maximo": 3}


def test_conta_acima_do_limite_continua_lendo_e_trabalhando(
    db_session, equipe, criar_empresa, limites
):
    """O limite ligado (ou reduzido) depois: nada some nem é bloqueado, só
    não entra empresa nova."""
    admin, headers = equipe
    segunda = criar_empresa("Cliente Antigo", conta_id=admin.conta_id)
    terceira = criar_empresa("Cliente Mais Antigo", conta_id=admin.conta_id)
    limites(empresas_essencial=1)

    lista = client.get("/empresas", headers=headers["analista"])
    extratos = client.get(
        "/extratos", headers={**headers["analista"], CABECALHO_EMPRESA: str(terceira.id)}
    )
    configuracao = client.put(
        "/empresa/configuracoes",
        headers={**headers["analista"], CABECALHO_EMPRESA: str(segunda.id)},
        json={"tolerancia_dias": 2},
    )

    assert lista.status_code == 200 and len(lista.json()["itens"]) == 3
    assert extratos.status_code == 200
    assert configuracao.status_code == 200
    assert _cadastrar_empresa(headers["admin"]).status_code == 402
    assert _limites_do_me(headers["admin"])["empresas"] == {"usadas": 3, "maximo": 1}


# --- usuários -------------------------------------------------------------------------


def test_sem_limite_configurado_a_conta_convida(db_session, provedor, equipe):
    _, headers = equipe

    assert _convidar(headers["admin"]).status_code == 201
    assert _limites_do_me(headers["admin"])["usuarios"] == {"usados": 4, "maximo": None}


@pytest.mark.parametrize("quem", ["admin", "gestor"])
def test_no_limite_de_usuarios_o_convite_e_402_para_todos(
    db_session, provedor, equipe, limites, quem
):
    _, headers = equipe
    limites(usuarios_essencial=3)  # admin, gestor e analista

    resposta = _convidar(headers[quem])

    assert resposta.status_code == 402
    assert resposta.json() == LIMITE_USUARIOS
    assert provedor.enviadas == []


def test_o_convite_pendente_ocupa_a_vaga(db_session, provedor, equipe, limites):
    _, headers = equipe
    limites(usuarios_essencial=4)

    assert _convidar(headers["admin"]).status_code == 201
    assert _limites_do_me(headers["admin"])["usuarios"] == {"usados": 4, "maximo": 4}
    assert _convidar(headers["admin"]).status_code == 402


def test_reenviar_o_convite_para_o_mesmo_email_nao_ocupa_outra_vaga(
    db_session, provedor, equipe, limites
):
    _, headers = equipe
    limites(usuarios_essencial=4)
    email = f"{uuid.uuid4().hex[:12]}@convidado.com"
    assert _convidar(headers["admin"], email).status_code == 201

    assert _convidar(headers["gestor"], email).status_code == 201
    assert _limites_do_me(headers["admin"])["usuarios"] == {"usados": 4, "maximo": 4}


def test_o_aceite_nao_e_barrado_pelo_limite(db_session, provedor, equipe, limites):
    _, headers = equipe
    limites(usuarios_essencial=4)
    assert _convidar(headers["admin"]).status_code == 201
    token = re.search(r"#token=(\S+)", provedor.enviadas[-1].texto).group(1)

    aceite = client.post(
        "/convites/aceitar", json={"token": token, "nome": "Nova", "senha": "senha-forte-123"}
    )

    assert aceite.status_code == 201
    assert _limites_do_me(headers["admin"])["usuarios"] == {"usados": 4, "maximo": 4}


def test_removido_libera_a_vaga(db_session, provedor, equipe, limites):
    _, headers = equipe
    limites(usuarios_essencial=3)
    assert _convidar(headers["admin"]).status_code == 402

    analista = next(
        u for u in db_session.get(Conta, equipe[0].conta_id).usuarios if u.papel == "analista"
    )
    remover_usuario(db_session, analista)
    db_session.commit()

    assert _limites_do_me(headers["admin"])["usuarios"] == {"usados": 2, "maximo": 3}
    assert _convidar(headers["admin"]).status_code == 201


def test_limites_no_me_para_todos_os_papeis(db_session, equipe, limites):
    _, headers = equipe
    limites(empresas_essencial=2, usuarios_essencial=10)

    for quem in ("admin", "gestor", "analista"):
        assert _limites_do_me(headers[quem]) == {
            "empresas": {"usadas": 1, "maximo": 2},
            "usuarios": {"usados": 3, "maximo": 10},
        }
