"""Bloqueio do `POST /conciliacoes` pelo plano (issue #122), atrás de
`PLANO_BLOQUEIO_HABILITADO`: desligado não barra nada; ligado, só a primeira
conciliação de um extrato do banco, com 402, em "sem_plano" e
"teste_encerrado". Ler, decidir, exportar, fechar o mês e enviar extrato
seguem liberados. O resto da #122 está em tests/test_plano.py.
"""

import io
import logging
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_plano import SITUACOES, _definir_situacao

from app.core.config import settings
from app.models import Conciliacao, ExecucaoConciliacao
from main import app

client = TestClient(app)


@pytest.fixture
def conta(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """Administrador com uma empresa, e fábricas de extrato e de conciliação."""
    administrador = criar_usuario()
    token = gerar_token(administrador.empresa_id, usuario_id=administrador.id)
    headers = {"Authorization": f"Bearer {token}"}

    def extrato(origem, itens=(("10.00", "a"), ("20.00", "b"))):
        novo = criar_extrato_da_empresa(administrador.empresa_id, origem)
        inserir_lancamentos(novo, list(itens))
        return novo

    def conciliar(banco, sistema):
        return client.post(
            "/conciliacoes",
            headers=headers,
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )

    def situacao(nome):
        _definir_situacao(db_session, administrador.empresa_id, nome)

    return SimpleNamespace(
        empresa_id=administrador.empresa_id,
        headers=headers,
        extrato=extrato,
        conciliar=conciliar,
        situacao=situacao,
    )


def _execucoes(db_session, banco) -> int:
    db_session.expire_all()
    return db_session.scalar(
        select(func.count()).where(ExecucaoConciliacao.extrato_banco_id == banco.id)
    )


@pytest.fixture
def bloqueio_ligado(monkeypatch):
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)


@pytest.mark.parametrize("situacao", SITUACOES)
def test_bloqueio_desligado_nao_barra_nenhuma_situacao(db_session, conta, situacao):
    assert settings.plano_bloqueio_habilitado is False
    conta.situacao(situacao)

    resposta = conta.conciliar(conta.extrato("banco"), conta.extrato("sistema"))

    assert resposta.status_code == 201


@pytest.mark.parametrize(
    ("situacao", "detalhe"),
    [
        ("sem_plano", "Escolha um plano para começar a conciliar."),
        (
            "teste_encerrado",
            "Seu período de teste acabou. Escolha um plano para fazer novas conciliações.",
        ),
    ],
)
def test_bloqueio_ligado_barra_a_primeira_conciliacao_com_402(
    db_session, conta, bloqueio_ligado, situacao, detalhe, caplog
):
    caplog.set_level(logging.INFO, logger="app")
    conta.situacao(situacao)
    banco = conta.extrato("banco")

    resposta = conta.conciliar(banco, conta.extrato("sistema"))

    assert resposta.status_code == 402
    assert resposta.json() == {"detail": detalhe}
    assert _execucoes(db_session, banco) == 0
    assert (
        db_session.scalar(select(func.count()).where(Conciliacao.extrato_banco_id == banco.id)) == 0
    )
    assert "conciliacao resultado=bloqueada_pelo_plano" in caplog.text


@pytest.mark.parametrize("situacao", ["teste", "ativo"])
def test_bloqueio_ligado_libera_teste_e_ativo(db_session, conta, bloqueio_ligado, situacao):
    conta.situacao(situacao)

    assert conta.conciliar(conta.extrato("banco"), conta.extrato("sistema")).status_code == 201


@pytest.mark.parametrize("situacao", ["sem_plano", "teste_encerrado"])
def test_bloqueio_ligado_libera_reconciliar_um_extrato_do_banco_ja_conciliado(
    db_session, conta, monkeypatch, situacao
):
    banco, sistema = conta.extrato("banco"), conta.extrato("sistema")
    assert conta.conciliar(banco, sistema).status_code == 201
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)
    conta.situacao(situacao)

    mesmo_par = conta.conciliar(banco, sistema)
    nova_versao_do_sistema = conta.conciliar(banco, conta.extrato("sistema"))
    outro_banco = conta.conciliar(conta.extrato("banco"), sistema)

    assert mesmo_par.status_code == 201
    assert nova_versao_do_sistema.status_code == 201
    assert outro_banco.status_code == 402
    assert _execucoes(db_session, banco) == 3


def test_bloqueio_ligado_nao_esconde_os_erros_de_validacao(db_session, conta, bloqueio_ligado):
    conta.situacao("sem_plano")
    sistema = conta.extrato("sistema")

    trocado = conta.conciliar(sistema, conta.extrato("banco"))

    assert trocado.status_code == 422


def test_bloqueio_ligado_libera_ler_decidir_exportar_fechar_e_enviar(
    db_session, conta, monkeypatch, criar_extrato_da_empresa, inserir_lancamentos
):
    banco = conta.extrato("banco", [("10.00", "a"), ("99.00", "so banco")])
    sistema = conta.extrato("sistema", [("10.00", "a")])
    for extrato in (banco, sistema):
        extrato.periodo_inicio = extrato.periodo_fim = date(2026, 9, 5)
    db_session.commit()
    assert conta.conciliar(banco, sistema).status_code == 201
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)
    conta.situacao("teste_encerrado")
    h = conta.headers

    listagem = client.get(f"/conciliacoes/{banco.id}", headers=h)
    chave = next(i["chave"] for i in listagem.json()["itens"] if i["status"] != "match_exato")
    decisao = client.post(
        f"/conciliacoes/{banco.id}/decisoes",
        headers=h,
        json={"chave": chave, "tipo": "justificada", "texto": "lançamento do banco conferido"},
    )
    exportacao = client.get(f"/conciliacoes/{banco.id}/exportar", headers=h)
    execucoes = client.get("/execucoes", headers=h)
    fechamento = client.post("/fechamentos", headers=h, json={"competencia": "2026-09"})
    envio = client.post(
        "/extratos/upload",
        headers=h,
        data={"origem": "banco"},
        files={"arquivo": ("e.csv", io.BytesIO(b"data,valor,descricao\n2026-09-05,1.00,x\n"))},
    )

    assert listagem.status_code == 200
    assert decisao.status_code == 200, decisao.text
    assert exportacao.status_code == 200
    assert execucoes.status_code == 200
    assert fechamento.status_code == 201, fechamento.text
    assert envio.status_code == 201
