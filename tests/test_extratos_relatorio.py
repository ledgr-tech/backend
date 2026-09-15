"""Testes do endpoint GET /extratos/{extrato_id} (issue #13): relatório de
validação de linhas — status, quantidade de lançamentos válidos e a lista
de erros por linha/transação.

Via TestClient real. Precisa de Postgres real (consulta Extrato/LinhaInvalida
de verdade) — usa a fixture `db_session`/`criar_empresa` de tests/conftest.py,
que pula estes testes quando não há Postgres real disponível (ver comentário
lá).
"""

import uuid

from fastapi.testclient import TestClient

from app.models import Extrato
from app.services.normalizacao import normalizar_extrato
from main import app

client = TestClient(app)


def test_get_extrato_com_erros_retorna_relatorio_correto(db_session, criar_empresa):
    empresa = criar_empresa()
    extrato = Extrato(
        empresa_id=empresa.id,
        nome_arquivo="extrato.csv",
        formato="csv",
        tamanho_bytes=0,
        status="pendente",
    )
    db_session.add(extrato)
    db_session.commit()

    conteudo = (
        b"data,valor,descricao\n"
        b"2026-09-05,1500.00,Deposito ok\n"
        b"2026-09-06,NAO_E_NUMERO,Linha quebrada\n"
    )
    normalizar_extrato(extrato.id, "csv", conteudo)

    response = client.get(f"/extratos/{extrato.id}")

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["extrato_id"] == str(extrato.id)
    assert corpo["status"] == "concluido_com_erros"
    assert corpo["quantidade_lancamentos"] == 1
    assert corpo["erros"] == [
        {"identificador": "3", "motivo": "Valor inválido no CSV: 'NAO_E_NUMERO'"}
    ]


def test_get_extrato_sem_erros_retorna_lista_de_erros_vazia(db_session, criar_empresa):
    empresa = criar_empresa()
    extrato = Extrato(
        empresa_id=empresa.id,
        nome_arquivo="extrato.csv",
        formato="csv",
        tamanho_bytes=0,
        status="pendente",
    )
    db_session.add(extrato)
    db_session.commit()

    conteudo = b"data,valor,descricao\n2026-09-05,1500.00,Deposito ok\n"
    normalizar_extrato(extrato.id, "csv", conteudo)

    response = client.get(f"/extratos/{extrato.id}")

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["status"] == "concluido"
    assert corpo["erros"] == []


def test_get_extrato_inexistente_retorna_404(db_session):
    response = client.get(f"/extratos/{uuid.uuid4()}")

    assert response.status_code == 404
