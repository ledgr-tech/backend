import io

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

# Estes testes cobrem só a validação de extensão/tamanho (issue #7), que
# acontece antes de qualquer acesso ao banco — por isso rodam sem precisar
# de um Postgres real (ver comentário no ci.yml). O caso de sucesso (upload
# válido persistindo no banco) é o critério de aceite manual da issue,
# verificado via Swagger/Postman contra um Postgres real.


def test_upload_rejeita_extensao_nao_suportada():
    arquivo = io.BytesIO(b"conteudo qualquer")
    response = client.post(
        "/extratos/upload",
        data={"empresa_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"},
        files={"arquivo": ("extrato.pdf", arquivo, "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_rejeita_arquivo_maior_que_5mb():
    conteudo_grande = b"0" * (5 * 1024 * 1024 + 1)
    arquivo = io.BytesIO(conteudo_grande)
    response = client.post(
        "/extratos/upload",
        data={"empresa_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"},
        files={"arquivo": ("extrato.csv", arquivo, "text/csv")},
    )
    assert response.status_code == 413


def test_upload_exige_empresa_id():
    arquivo = io.BytesIO(b"data,valor\n2026-01-01,100.00")
    response = client.post(
        "/extratos/upload",
        files={"arquivo": ("extrato.csv", arquivo, "text/csv")},
    )
    assert response.status_code == 422
