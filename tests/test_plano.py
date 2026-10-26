"""Plano da conta e período de teste (issues #122 e #133): a situação no `GET /me`,
o `POST /empresa/plano` e a virada do teste no relógio congelado. O bloqueio
do `POST /conciliacoes` está em tests/test_plano_bloqueio.py.

O relógio congelado é o `app.core.auth._agora` da #75. Com ele congelado, os
tokens levam `auth_time` no mesmo instante, senão a sessão não valeria.
"""

import logging
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.core.plano import DURACAO_TESTE, PLANOS, guarda_arquivo_original, situacao_da_conta
from app.models import Conta
from main import app

client = TestClient(app)

T0 = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
SITUACOES = ["sem_plano", "teste", "teste_encerrado", "ativo"]


def _congelar(monkeypatch, instante: datetime) -> None:
    monkeypatch.setattr(auth, "_agora", lambda: instante)


@pytest.fixture
def cabecalho(gerar_token):
    """Cabeçalho do usuário, com `auth_time` no relógio da aplicação
    (congelado ou não)."""

    def _cabecalho(usuario) -> dict[str, str]:
        auth_time = math.floor(auth._agora().timestamp())
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, auth_time=auth_time)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _definir_situacao(db_session, conta_id, situacao: str) -> None:
    empresa = db_session.get(Conta, conta_id)
    agora = auth._agora()
    empresa.assinatura_ativa = situacao == "ativo"
    if situacao in ("sem_plano", "ativo"):
        empresa.plano, empresa.teste_termina_em = None, None
    else:
        empresa.plano = "padrao"
        fim = timedelta(days=3) if situacao == "teste" else -timedelta(seconds=1)
        empresa.teste_termina_em = agora + fim
    db_session.commit()


def _me(headers):
    resposta = client.get("/me", headers=headers)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def _escolher(headers, plano):
    return client.post("/empresa/plano", headers=headers, json={"plano": plano})


def _empresa(db_session, conta_id) -> Conta:
    db_session.expire_all()
    return db_session.get(Conta, conta_id)


# --- a regra -------------------------------------------------------------------


def test_planos_escolhiveis_sao_os_tres_sem_volume():
    assert PLANOS == ("essencial", "padrao", "avancado")


@pytest.mark.parametrize(
    ("ativa", "plano", "fim", "esperada"),
    [
        (False, None, None, "sem_plano"),
        (False, "padrao", T0 + timedelta(microseconds=1), "teste"),
        (False, "padrao", T0, "teste_encerrado"),
        (False, "padrao", T0 - timedelta(days=1), "teste_encerrado"),
        (True, None, None, "ativo"),
        (True, "padrao", T0 - timedelta(days=1), "ativo"),
    ],
)
def test_situacao_da_conta(ativa, plano, fim, esperada):
    empresa = SimpleNamespace(assinatura_ativa=ativa, plano=plano, teste_termina_em=fim)

    assert situacao_da_conta(empresa, T0) == esperada


# --- GET /me ------------------------------------------------------------------------


def test_empresa_nova_comeca_sem_plano(db_session, criar_usuario, cabecalho):
    corpo = _me(cabecalho(criar_usuario()))

    assert (corpo["plano"], corpo["teste_termina_em"], corpo["situacao"]) == (
        None,
        None,
        "sem_plano",
    )


@pytest.mark.parametrize("situacao", SITUACOES)
def test_me_devolve_a_situacao_para_o_administrador_e_o_convidado(
    db_session, criar_usuario, cabecalho, situacao
):
    administrador = criar_usuario()
    convidado = criar_usuario(conta_id=administrador.conta_id, nome="Bruno Convidado")
    _definir_situacao(db_session, administrador.conta_id, situacao)
    empresa = _empresa(db_session, administrador.conta_id)

    for usuario in (administrador, convidado):
        corpo = _me(cabecalho(usuario))
        assert corpo["situacao"] == situacao
        assert corpo["plano"] == empresa.plano
        if empresa.teste_termina_em is None:
            assert corpo["teste_termina_em"] is None
        else:
            devolvido = datetime.fromisoformat(corpo["teste_termina_em"])
            assert devolvido.utcoffset() == timedelta(0)
            assert devolvido == empresa.teste_termina_em


# --- POST /empresa/plano ---------------------------------------------------------


def test_primeira_escolha_comeca_o_teste_de_14_dias(
    db_session, criar_usuario, cabecalho, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    _congelar(monkeypatch, T0)
    administrador = criar_usuario()

    resposta = _escolher(cabecalho(administrador), "essencial")

    assert resposta.status_code == 200
    fim = T0 + DURACAO_TESTE
    assert resposta.json() == {
        "plano": "essencial",
        "teste_termina_em": fim.isoformat().replace("+00:00", "Z"),
        "situacao": "teste",
    }
    empresa = _empresa(db_session, administrador.conta_id)
    assert (empresa.plano, empresa.teste_termina_em) == ("essencial", fim)
    assert _me(cabecalho(administrador))["situacao"] == "teste"
    assert "escolha_plano resultado=teste_iniciado" in caplog.text


def test_trocar_de_plano_nao_muda_o_fim_do_teste(db_session, criar_usuario, cabecalho, monkeypatch):
    _congelar(monkeypatch, T0)
    administrador = criar_usuario()
    _escolher(cabecalho(administrador), "essencial")

    _congelar(monkeypatch, T0 + timedelta(days=5))
    resposta = _escolher(cabecalho(administrador), "avancado")

    assert resposta.status_code == 200
    assert resposta.json()["plano"] == "avancado"
    assert resposta.json()["situacao"] == "teste"
    empresa = _empresa(db_session, administrador.conta_id)
    assert (empresa.plano, empresa.teste_termina_em) == ("avancado", T0 + DURACAO_TESTE)


def test_trocar_de_plano_com_o_teste_encerrado_nao_reabre_o_teste(
    db_session, criar_usuario, cabecalho
):
    administrador = criar_usuario()
    _definir_situacao(db_session, administrador.conta_id, "teste_encerrado")
    fim = _empresa(db_session, administrador.conta_id).teste_termina_em

    resposta = _escolher(cabecalho(administrador), "essencial")

    assert resposta.json()["situacao"] == "teste_encerrado"
    assert _empresa(db_session, administrador.conta_id).teste_termina_em == fim


def test_mesmo_plano_de_novo_nao_muda_nada(
    db_session, criar_usuario, cabecalho, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    _congelar(monkeypatch, T0)
    administrador = criar_usuario()
    primeira = _escolher(cabecalho(administrador), "padrao").json()
    antes = _empresa(db_session, administrador.conta_id).atualizado_em

    _congelar(monkeypatch, T0 + timedelta(days=1))
    segunda = _escolher(cabecalho(administrador), "padrao")

    assert segunda.status_code == 200
    assert segunda.json() == primeira
    empresa = _empresa(db_session, administrador.conta_id)
    assert (empresa.plano, empresa.teste_termina_em) == ("padrao", T0 + DURACAO_TESTE)
    assert empresa.atualizado_em == antes
    assert "escolha_plano resultado=mesmo_plano" in caplog.text


def test_com_assinatura_ativa_a_troca_vale_e_a_situacao_continua_ativo(
    db_session, criar_usuario, cabecalho
):
    administrador = criar_usuario()
    _definir_situacao(db_session, administrador.conta_id, "ativo")

    primeira = _escolher(cabecalho(administrador), "essencial").json()
    troca = _escolher(cabecalho(administrador), "avancado").json()

    assert (primeira["situacao"], troca["situacao"]) == ("ativo", "ativo")
    assert troca["plano"] == "avancado"
    assert _me(cabecalho(administrador))["situacao"] == "ativo"


@pytest.mark.parametrize(
    "corpo",
    [{"plano": "volume"}, {"plano": "Essencial"}, {"plano": ""}, {"plano": None}, {}],
    ids=["volume", "maiuscula", "vazio", "nulo", "ausente"],
)
def test_plano_fora_da_lista_da_422_no_campo(db_session, criar_usuario, cabecalho, corpo):
    administrador = criar_usuario()

    resposta = client.post("/empresa/plano", headers=cabecalho(administrador), json=corpo)

    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["loc"] == ["body", "plano"]
    assert _empresa(db_session, administrador.conta_id).plano is None


def test_so_o_administrador_escolhe_o_plano(db_session, criar_usuario, cabecalho):
    administrador = criar_usuario()
    convidado = criar_usuario(conta_id=administrador.conta_id, nome="Bruno Convidado")

    resposta = _escolher(cabecalho(convidado), "essencial")

    assert resposta.status_code == 403
    assert resposta.json()["detail"] == "Só o admin da conta pode fazer isso."
    assert _empresa(db_session, administrador.conta_id).plano is None


def test_sem_token_da_401():
    assert client.post("/empresa/plano", json={"plano": "essencial"}).status_code == 401


# --- virada do teste ------------------------------------------------------------------


def test_teste_vira_encerrado_no_instante_de_teste_termina_em(
    db_session, criar_usuario, cabecalho, monkeypatch
):
    _congelar(monkeypatch, T0)
    administrador = criar_usuario()
    _escolher(cabecalho(administrador), "padrao")
    fim = T0 + DURACAO_TESTE

    _congelar(monkeypatch, fim - timedelta(microseconds=1))
    assert _me(cabecalho(administrador))["situacao"] == "teste"

    _congelar(monkeypatch, fim)
    assert _me(cabecalho(administrador))["situacao"] == "teste_encerrado"


# --- Arquivo original do extrato (issue #139, ADR-023) ---------------------------


@pytest.mark.parametrize(
    "plano, esperado",
    [("avancado", True), ("padrao", False), ("essencial", False), (None, False)],
)
def test_so_o_avancado_guarda_o_arquivo_original(plano, esperado):
    conta = SimpleNamespace(plano=plano, teste_termina_em=None, assinatura_ativa=False)
    assert guarda_arquivo_original(conta) is esperado


def test_avancado_guarda_em_qualquer_situacao():
    passado = datetime(2026, 1, 1, tzinfo=UTC)
    for conta in (
        SimpleNamespace(plano="avancado", teste_termina_em=passado, assinatura_ativa=False),
        SimpleNamespace(plano="avancado", teste_termina_em=passado, assinatura_ativa=True),
    ):
        assert guarda_arquivo_original(conta) is True
