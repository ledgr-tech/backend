"""Testes de volume do POST /conciliacoes com Postgres real (issues #19 e #74).

Insere os lançamentos direto no banco, em lote, sem passar pelo upload, com
75% de sobreposição.

Detector de regressão: teto RELATIVO (issue #74). Antes, cada tamanho tinha um
teto absoluto de 30 s, e em 25/09 o caso de 20 mil passou dele na suíte
completa local sem nenhuma mudança de código (sozinho, levou poucos segundos).
Os runners do GitHub Actions têm desempenho variável, então um teto absoluto
mede a máquina, não o código. Agora o teste de tempo mede 5 mil e 20 mil por
lado NA MESMA execução, depois de uma conciliação pequena de aquecimento, e
exige razão de tempo menor que 8: 20 mil é 4 vezes 5 mil, então crescimento
linear dá razão perto de 4 e quadrático, perto de 16. O 8 deixa o dobro de
folga para ruído e ainda fica longe do quadrático. Uma máquina lenta deixa os
dois tempos lentos e a razão quase não muda.

O teto absoluto ficou só como rede contra travamento: 120 s por chamada,
folgado de propósito. Ele não é o detector de regressão.

Os testes de corretude por tamanho (5, 10 e 20 mil) conferem as contagens,
sem assert de tempo. `verificar_crescimento_linear` tem teste próprio, sem
banco e sem o marcador `volume`, que mostra que o critério pega um crescimento
quadrático.

Os testes com banco têm o marcador `volume` e rodam num job próprio da CI.
Rodar: `pytest -m volume -s tests/test_conciliacoes_volume.py` (`-s` mostra os
tempos).
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

# Rede contra travamento, não detector de regressão (ver docstring).
TETO_SEGURANCA_SEGUNDOS = 120.0
RAZAO_MAXIMA = 8
LOTE = 5_000


def verificar_crescimento_linear(
    tempo_menor: float, tempo_maior: float, fator_tamanho: float, razao_maxima: float
) -> float:
    """Levanta AssertionError quando `tempo_maior / tempo_menor` passa de
    `razao_maxima`. `fator_tamanho` é quantas vezes o caso maior é o menor:
    crescimento linear daria uma razão perto dele. Devolve a razão."""
    if tempo_menor <= 0:
        raise AssertionError(f"tempo do caso menor inválido: {tempo_menor:.3f}s")
    razao = tempo_maior / tempo_menor
    if razao >= razao_maxima:
        raise AssertionError(
            f"tempo cresceu {razao:.2f}x para {fator_tamanho:g}x o tamanho "
            f"(menor={tempo_menor:.3f}s, maior={tempo_maior:.3f}s, limite={razao_maxima:g}x; "
            f"linear daria perto de {fator_tamanho:g}x)"
        )
    return razao


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


def _montar_par(db_session, empresa_id, por_lado: int):
    """Extrato do banco e do sistema com `por_lado` lançamentos cada, 75% em
    comum. Devolve os dois ids."""
    sobreposicao = por_lado * 3 // 4
    ids = {}
    for origem in ("banco", "sistema"):
        extrato = Extrato(
            empresa_id=empresa_id,
            nome_arquivo="volume.csv",
            formato="csv",
            tamanho_bytes=0,
            origem=origem,
            status="concluido",
        )
        db_session.add(extrato)
        db_session.commit()
        ids[origem] = extrato.id

    comuns = range(sobreposicao)
    extras = range(sobreposicao, por_lado)
    _inserir_em_lote(
        db_session,
        _linhas(empresa_id, ids["banco"], comuns, 0)
        + _linhas(empresa_id, ids["banco"], extras, 10_000_000),
    )
    _inserir_em_lote(
        db_session,
        _linhas(empresa_id, ids["sistema"], comuns, 0)
        + _linhas(empresa_id, ids["sistema"], extras, 20_000_000),
    )
    return ids["banco"], ids["sistema"]


def _conciliar(headers, banco_id, sistema_id):
    """POST /conciliacoes e o tempo da chamada, em segundos."""
    inicio = time.perf_counter()
    response = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )
    return response, time.perf_counter() - inicio


# Critério do teto relativo (sem banco, sem o marcador volume)


def test_crescimento_linear_passa():
    assert verificar_crescimento_linear(1.0, 4.2, 4, RAZAO_MAXIMA) == pytest.approx(4.2)


def test_crescimento_quadratico_falha_com_os_numeros_na_mensagem():
    with pytest.raises(AssertionError) as erro:
        verificar_crescimento_linear(1.0, 16.0, 4, RAZAO_MAXIMA)

    mensagem = str(erro.value)
    assert "16.00x" in mensagem
    assert "menor=1.000s" in mensagem and "maior=16.000s" in mensagem
    assert "limite=8x" in mensagem


def test_tempo_do_caso_menor_zerado_falha():
    with pytest.raises(AssertionError):
        verificar_crescimento_linear(0.0, 1.0, 4, RAZAO_MAXIMA)


# Volume com HTTP e banco


@pytest.mark.volume
@pytest.mark.parametrize("por_lado", [5_000, 10_000, 20_000])
def test_post_conciliacoes_com_volume_conta_matches(
    por_lado, db_session, criar_empresa, auth_headers
):
    sobreposicao = por_lado * 3 // 4
    empresa_id = criar_empresa().id
    banco_id, sistema_id = _montar_par(db_session, empresa_id, por_lado)

    response, duracao = _conciliar(auth_headers(empresa_id), banco_id, sistema_id)
    print(f"\nTEMPO_POST_CONCILIACOES por_lado={por_lado} segundos={duracao:.2f}")

    assert response.status_code == 201
    corpo = response.json()
    assert corpo["match_exato"] == sobreposicao
    # As datas ciclam em só 28 dias (_linhas), então com milhares de "extras"
    # de cada lado praticamente todo dia tem gente dos dois lados — os
    # leftovers quase todos acham a data do outro lado sem o valor bater e
    # viram divergente_valor (issue #24), não sem_correspondencia. A soma das
    # 4 categorias de "não casou" é que é invariante.
    assert (
        corpo["sem_correspondencia"]
        + corpo["tarifa_bancaria"]
        + corpo["divergente_valor"]
        + corpo["divergente_data"]
    ) == 2 * (por_lado - sobreposicao)
    assert corpo["duplicado"] == 0
    assert corpo["total"] == sobreposicao + 2 * (por_lado - sobreposicao)

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Conciliacao)
            .where(Conciliacao.empresa_id == empresa_id)
        )
        == corpo["total"]
    )


@pytest.mark.volume
def test_post_conciliacoes_tempo_cresce_de_forma_aproximadamente_linear(
    db_session, criar_empresa, auth_headers
):
    empresa_id = criar_empresa().id
    headers = auth_headers(empresa_id)
    aquecimento = _montar_par(db_session, empresa_id, 200)
    pequeno = _montar_par(db_session, empresa_id, 5_000)
    grande = _montar_par(db_session, empresa_id, 20_000)

    resposta, _ = _conciliar(headers, *aquecimento)  # aquecimento, fora da medição
    assert resposta.status_code == 201
    resposta_5k, tempo_5k = _conciliar(headers, *pequeno)
    resposta_20k, tempo_20k = _conciliar(headers, *grande)
    print(
        f"\nTEMPO_POST_CONCILIACOES_RELATIVO 5k={tempo_5k:.2f}s 20k={tempo_20k:.2f}s "
        f"razao={tempo_20k / tempo_5k:.2f}"
    )

    assert resposta_5k.status_code == resposta_20k.status_code == 201
    for tempo in (tempo_5k, tempo_20k):
        assert tempo < TETO_SEGURANCA_SEGUNDOS, f"POST levou {tempo:.2f}s (travou?)"
    verificar_crescimento_linear(tempo_5k, tempo_20k, 4, RAZAO_MAXIMA)
