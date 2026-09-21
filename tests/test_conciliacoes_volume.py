"""Teste de volume do POST /conciliacoes com Postgres real (issue #19).

Insere os lançamentos direto no banco, em lote, sem passar pelo upload, com
75% de sobreposição. O teto de tempo é absoluto e folgado: o banco de teste
pode ter latência (Railway). Não há assert de razão entre tamanhos aqui, isso
é do núcleo puro (tests/test_matching_volume.py).

Cada tamanho imprime o tempo da chamada (visível com `pytest -s`), pra montar
a tabela de tempos do PR. Rodar: `pytest -s tests/test_conciliacoes_volume.py`.
"""

import hashlib
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from app.models import Conciliacao, Extrato, Lancamento
from main import app

client = TestClient(app)

TETO_POST_SEGUNDOS = 60.0
LOTE = 5_000


def _linhas(empresa_id, extrato_id, faixa: range, deslocamento_valor: int):
    """Um lançamento por índice da faixa. O valor (em centavos) é único por
    índice mais o deslocamento, então a chave (valor, data) nunca colide entre
    índices diferentes nem entre as faixas com deslocamentos diferentes."""
    linhas = []
    for indice in faixa:
        valor = Decimal(deslocamento_valor + indice) / 100
        linhas.append(
            {
                "empresa_id": empresa_id,
                "extrato_id": extrato_id,
                "data": date(2026, 9, 1) + timedelta(days=indice % 28),
                "valor": valor,
                "descricao": f"Lancamento {indice}",
                "tipo": "credito",
                "hash_dedup": hashlib.sha256(
                    f"{extrato_id}|{deslocamento_valor}|{indice}".encode()
                ).hexdigest(),
                "ocorrencia": 1,
            }
        )
    return linhas


def _inserir_em_lote(db_session, linhas) -> None:
    for inicio in range(0, len(linhas), LOTE):
        db_session.execute(insert(Lancamento), linhas[inicio : inicio + LOTE])
    db_session.commit()


@pytest.mark.parametrize("por_lado", [5_000, 10_000, 20_000])
def test_post_conciliacoes_com_volume_conta_matches_e_fica_abaixo_do_teto(
    por_lado, db_session, criar_empresa, auth_headers
):
    sobreposicao = por_lado * 3 // 4
    empresa = criar_empresa()
    extratos = {}
    for origem in ("banco", "sistema"):
        extrato = Extrato(
            empresa_id=empresa.id,
            nome_arquivo="volume.csv",
            formato="csv",
            tamanho_bytes=0,
            origem=origem,
            status="concluido",
        )
        db_session.add(extrato)
        db_session.commit()
        extratos[origem] = extrato
    empresa_id = empresa.id
    banco_id, sistema_id = extratos["banco"].id, extratos["sistema"].id

    comuns = range(sobreposicao)
    extras = range(sobreposicao, por_lado)
    _inserir_em_lote(
        db_session,
        _linhas(empresa_id, banco_id, comuns, 0)
        + _linhas(empresa_id, banco_id, extras, 10_000_000),
    )
    _inserir_em_lote(
        db_session,
        _linhas(empresa_id, sistema_id, comuns, 0)
        + _linhas(empresa_id, sistema_id, extras, 20_000_000),
    )

    inicio = time.perf_counter()
    response = client.post(
        "/conciliacoes",
        headers=auth_headers(empresa_id),
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )
    duracao = time.perf_counter() - inicio
    print(f"\nTEMPO_POST_CONCILIACOES por_lado={por_lado} segundos={duracao:.2f}")

    assert response.status_code == 201
    corpo = response.json()
    assert corpo["match_exato"] == sobreposicao
    assert corpo["sem_correspondencia"] == 2 * (por_lado - sobreposicao)
    assert corpo["duplicado"] == 0
    assert corpo["total"] == sobreposicao + 2 * (por_lado - sobreposicao)
    assert duracao < TETO_POST_SEGUNDOS, f"POST levou {duracao:.2f}s"

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Conciliacao)
            .where(Conciliacao.empresa_id == empresa_id)
        )
        == corpo["total"]
    )
