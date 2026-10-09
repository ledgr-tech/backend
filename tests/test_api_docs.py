"""API_DOCS_HABILITADO (issue #102): em produção, /docs e /openapi.json somem.

O `main` monta a app no import, então cada valor da variável roda num
subprocesso próprio, como o uvicorn faria no boot.
"""

import json
import os
import subprocess
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROTAS = ("/docs", "/redoc", "/openapi.json", "/health")


def _status_das_rotas(api_docs_habilitado: str | None) -> dict[str, int]:
    env = {k: v for k, v in os.environ.items() if k != "API_DOCS_HABILITADO"}
    if api_docs_habilitado is not None:
        env["API_DOCS_HABILITADO"] = api_docs_habilitado
    codigo = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "from main import app\n"
        "cliente = TestClient(app)\n"
        f"print(json.dumps({{rota: cliente.get(rota).status_code for rota in {ROTAS!r}}}))\n"
    )
    processo = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert processo.returncode == 0, processo.stderr
    return json.loads(processo.stdout.strip().splitlines()[-1])


def test_com_false_docs_e_openapi_respondem_404_e_health_200():
    status = _status_das_rotas("false")

    assert status == {"/docs": 404, "/redoc": 404, "/openapi.json": 404, "/health": 200}


@pytest.mark.parametrize("valor", [None, "true"])
def test_ligado_por_padrao(valor):
    status = _status_das_rotas(valor)

    assert status == {"/docs": 200, "/redoc": 200, "/openapi.json": 200, "/health": 200}
