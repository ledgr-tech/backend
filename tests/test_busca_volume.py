"""Volume do GET /busca (issue #220) com Postgres real.

A busca parte das linhas de `conciliacoes` dos pares em vigor da empresa e
confere o termo nos dois lançamentos de cada linha, sem índice próprio
(app/services/busca.py explica por que um índice trigram na descrição não
seria usado). O custo é linear nas linhas conciliadas da empresa. Este teste
garante isso com a régua de tests/test_conciliacoes_volume.py: mede 5 mil e
20 mil lançamentos por lado na mesma execução e exige razão de tempo menor
que 8 (linear dá perto de 4, quadrático perto de 16). O teto absoluto é só
rede contra travamento.

Rodar: `pytest -m volume -s tests/test_busca_volume.py` (`-s` mostra os tempos).
"""

import time

import pytest
from fastapi.testclient import TestClient
from test_conciliacoes_volume import (
    RAZAO_MAXIMA,
    _conciliar,
    _montar_par,
    verificar_crescimento_linear,
)

from main import app

pytestmark = pytest.mark.volume

client = TestClient(app)

TETO_SEGURANCA_SEGUNDOS = 30.0
# Um de cada jeito: texto, valor e data sem ano. Os três passam pelas mesmas
# linhas; o texto é o mais caro (translate e LIKE em cada descrição).
TERMOS = ("lancamento 1234", "12,34", "05/09")


def _empresa_com_conciliacao(criar_empresa, db_session, auth_headers, por_lado):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    banco_id, sistema_id = _montar_par(db_session, empresa.id, por_lado)
    resposta, _ = _conciliar(headers, banco_id, sistema_id)
    assert resposta.status_code == 201, resposta.text
    return headers


def _tempo_da_busca(headers) -> float:
    """O pior tempo entre os termos, cada um a melhor de três chamadas."""
    piores = []
    for termo in TERMOS:
        tempos = []
        for _ in range(3):
            inicio = time.perf_counter()
            resposta = client.get("/busca", headers=headers, params={"q": termo})
            tempos.append(time.perf_counter() - inicio)
            assert resposta.status_code == 200
            assert resposta.json()["total"] > 0
        piores.append(min(tempos))
    return max(piores)


def test_tempo_da_busca_cresce_linear_com_as_linhas_conciliadas(
    db_session, criar_empresa, auth_headers
):
    pequena = _empresa_com_conciliacao(criar_empresa, db_session, auth_headers, 5_000)
    grande = _empresa_com_conciliacao(criar_empresa, db_session, auth_headers, 20_000)
    _tempo_da_busca(pequena)  # aquecimento

    tempo_pequena = _tempo_da_busca(pequena)
    tempo_grande = _tempo_da_busca(grande)
    print(f"\nbusca: 5 mil por lado {tempo_pequena:.3f}s, 20 mil por lado {tempo_grande:.3f}s")

    assert tempo_grande < TETO_SEGURANCA_SEGUNDOS
    verificar_crescimento_linear(tempo_pequena, tempo_grande, 4, RAZAO_MAXIMA)
