"""Teste de integração do fluxo completo de upload -> normalização (issue #12):
sobe o endpoint real via TestClient e confirma que, ao final, o extrato
está com status="concluido" e quantidade_lancamentos correta.

Nota importante: o TestClient do Starlette/FastAPI executa BackgroundTasks
de forma síncrona antes de devolver a resposta do teste — não dá pra testar
aqui "a resposta HTTP não espera a task" por tempo de execução. Isso é
testado pela resposta em si continuar retornando status="pendente" (ver
tests/test_extratos_upload.py, que cobre a validação síncrona da #7); o
resultado *final* da task (status="concluido", quantidade_lancamentos) é
o que este arquivo verifica, consultando o banco depois da chamada.

Precisa de Postgres real (grava Extrato/Lancamento de verdade) — usa a
fixture `db_session`/`criar_empresa` de tests/conftest.py, que pula estes
testes quando não há Postgres real disponível (CI hoje).
"""

import io
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.models import Extrato, Lancamento
from main import app

client = TestClient(app)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_upload_ofx_valido_normaliza_e_fica_concluido(db_session, criar_empresa, auth_headers):
    empresa = criar_empresa()
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    response = client.post(
        "/extratos/upload",
        headers=auth_headers(empresa.id),
        files={"arquivo": ("extrato.ofx", io.BytesIO(conteudo), "application/octet-stream")},
    )

    assert response.status_code == 201
    corpo = response.json()
    assert corpo["status"] == "pendente"  # resposta HTTP não espera a task (issue #12)

    extrato_id = uuid.UUID(corpo["extrato_id"])
    db_session.expire_all()
    extrato = db_session.get(Extrato, extrato_id)
    assert extrato.status == "concluido"
    assert extrato.quantidade_lancamentos == 2

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato_id).all()
    assert len(lancamentos) == 2


def test_upload_csv_valido_normaliza_e_fica_concluido(db_session, criar_empresa, auth_headers):
    empresa = criar_empresa()
    conteudo = (FIXTURES_DIR / "extrato_valido.csv").read_bytes()

    response = client.post(
        "/extratos/upload",
        headers=auth_headers(empresa.id),
        files={"arquivo": ("extrato.csv", io.BytesIO(conteudo), "text/csv")},
    )

    assert response.status_code == 201
    extrato_id = uuid.UUID(response.json()["extrato_id"])

    db_session.expire_all()
    extrato = db_session.get(Extrato, extrato_id)
    assert extrato.status == "concluido"
    assert extrato.quantidade_lancamentos == 3
