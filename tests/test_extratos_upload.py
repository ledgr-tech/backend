import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import Extrato
from main import app

client = TestClient(app)

EMPRESA_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

# Estes testes cobrem só a validação de extensão/tamanho (issue #7), que
# acontece antes de qualquer acesso ao banco — por isso rodam sem precisar
# de um Postgres real (ver comentário no ci.yml). O caso de sucesso (upload
# válido persistindo no banco) é o critério de aceite manual da issue,
# verificado via Swagger/Postman contra um Postgres real.


def test_upload_rejeita_extensao_nao_suportada(auth_headers):
    arquivo = io.BytesIO(b"conteudo qualquer")
    response = client.post(
        "/extratos/upload",
        headers=auth_headers(EMPRESA_ID),
        data={"origem": "banco"},
        files={"arquivo": ("extrato.pdf", arquivo, "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_rejeita_arquivo_maior_que_5mb(auth_headers):
    conteudo_grande = b"0" * (5 * 1024 * 1024 + 1)
    arquivo = io.BytesIO(conteudo_grande)
    response = client.post(
        "/extratos/upload",
        headers=auth_headers(EMPRESA_ID),
        data={"origem": "banco"},
        files={"arquivo": ("extrato.csv", arquivo, "text/csv")},
    )
    assert response.status_code == 413


def test_upload_sem_origem_retorna_422(auth_headers):
    response = client.post(
        "/extratos/upload",
        headers=auth_headers(EMPRESA_ID),
        files={"arquivo": ("extrato.csv", io.BytesIO(b"x"), "text/csv")},
    )
    assert response.status_code == 422


def test_upload_com_origem_invalida_retorna_422(auth_headers):
    response = client.post(
        "/extratos/upload",
        headers=auth_headers(EMPRESA_ID),
        data={"origem": "outra"},
        files={"arquivo": ("extrato.csv", io.BytesIO(b"x"), "text/csv")},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("origem", ["banco", "sistema"])
def test_upload_com_origem_valida_persiste_e_devolve_no_detalhe(
    origem, db_session, criar_empresa, auth_headers
):
    empresa = criar_empresa()
    conteudo = b"data,valor,descricao\n2026-09-05,1500.00,Deposito ok\n"

    response = client.post(
        "/extratos/upload",
        headers=auth_headers(empresa.id),
        data={"origem": origem},
        files={"arquivo": ("extrato.csv", io.BytesIO(conteudo), "text/csv")},
    )

    assert response.status_code == 201
    extrato_id = response.json()["extrato_id"]
    db_session.expire_all()
    assert db_session.get(Extrato, uuid.UUID(extrato_id)).origem == origem

    detalhe = client.get(f"/extratos/{extrato_id}", headers=auth_headers(empresa.id))
    assert detalhe.json()["origem"] == origem
