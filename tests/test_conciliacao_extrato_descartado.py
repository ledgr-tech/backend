"""Conciliar um extrato descartado no meio (issue #185): a rota responde 404 e
não deixa execução nem conciliação órfã. O descarte em si está em
tests/test_extratos_descarte.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import conciliacoes
from app.core.database import engine
from app.models import ExecucaoConciliacao, Extrato
from app.services.matching import ExtratoInexistenteError, conciliar_extratos
from main import app

client = TestClient(app)


@pytest.fixture
def par(criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a")])
    inserir_lancamentos(sistema, [("10.00", "a")])
    return empresa, auth_headers(empresa.id), banco, sistema


def _execucoes(db_session, banco_id) -> int:
    db_session.expire_all()
    return db_session.scalar(
        select(func.count())
        .select_from(ExecucaoConciliacao)
        .where(ExecucaoConciliacao.extrato_banco_id == banco_id)
    )


def test_conciliacao_depois_do_descarte_da_404_sem_execucao_orfa(db_session, par, monkeypatch):
    """O extrato do sistema é descartado depois da validação da rota e antes de
    a conciliação gravar: a rota responde 404, e nenhuma execução fica gravada."""
    _, headers, banco, sistema = par
    # O sistema ainda não foi usado: nada o impede de ser descartado.
    original = conciliacoes._garantir_mes_aberto

    def descarta_e_segue(db, empresa_id, extrato_banco):
        assert client.delete(f"/extratos/{sistema.id}", headers=headers).status_code == 204
        original(db, empresa_id, extrato_banco)

    monkeypatch.setattr(conciliacoes, "_garantir_mes_aberto", descarta_e_segue)
    resposta = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Extrato não encontrado."}
    assert _execucoes(db_session, banco.id) == 0
    assert db_session.get(Extrato, banco.id) is not None


def test_conciliar_extratos_com_extrato_inexistente_levanta_e_nao_grava(db_session, par):
    empresa, _, banco, _ = par

    with Session(engine) as sessao, pytest.raises(ExtratoInexistenteError):
        conciliar_extratos(sessao, empresa.id, banco.id, uuid.uuid4())

    assert _execucoes(db_session, banco.id) == 0
