"""Testes do POST /conciliacoes (issue #16, ADR-006 e ADR-007) contra Postgres
real, mais o teste ponta a ponta do critério de aceite: par sintético gerado
por scripts/gerar_extratos_sinteticos.py --par, enviado via upload.

O TestClient roda as BackgroundTasks antes de devolver a resposta (ver
tests/test_extratos_upload_normalizacao.py), então os extratos enviados pelo
upload já estão processados quando o teste continua.
"""

import hashlib
import io
import re
import threading
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.models import Conciliacao, Extrato, Lancamento
from app.services import matching
from main import app
from scripts.gerar_extratos_sinteticos import main as gerar_extratos

client = TestClient(app)


@pytest.fixture
def criar_extrato_da_empresa(db_session):
    """Extrato com status/origem à escolha numa empresa já existente (a
    fábrica `criar_extrato` do conftest cria uma empresa nova a cada chamada)."""

    def _criar(empresa_id, origem: str, status: str = "concluido") -> Extrato:
        extrato = Extrato(
            empresa_id=empresa_id,
            nome_arquivo="extrato.csv",
            formato="csv",
            tamanho_bytes=0,
            origem=origem,
            status=status,
        )
        db_session.add(extrato)
        db_session.commit()
        return extrato

    return _criar


@pytest.fixture
def inserir_lancamentos(db_session):
    def _inserir(extrato: Extrato, itens: list[tuple[str, str]]) -> list[Lancamento]:
        """itens: (valor, descricao), sempre na mesma data. Cada item repetido
        recebe a próxima ocorrência, como faz a normalização (ADR-006)."""
        vistos: dict[tuple[str, str], int] = {}
        criados = []
        for valor, descricao in itens:
            vistos[(valor, descricao)] = vistos.get((valor, descricao), 0) + 1
            lancamento = Lancamento(
                empresa_id=extrato.empresa_id,
                extrato_id=extrato.id,
                data=date(2026, 9, 5),
                valor=Decimal(valor),
                descricao=descricao,
                tipo="credito" if Decimal(valor) > 0 else "debito",
                hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                ocorrencia=vistos[(valor, descricao)],
            )
            db_session.add(lancamento)
            criados.append(lancamento)
        db_session.commit()
        return criados

    return _inserir


def _post(headers, banco_id, sistema_id):
    return client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )


def _conciliacoes(db_session, banco_id, sistema_id):
    db_session.expire_all()
    return (
        db_session.query(Conciliacao)
        .filter_by(extrato_banco_id=banco_id, extrato_sistema_id=sistema_id)
        .all()
    )


def test_post_feliz_grava_linhas_e_devolve_contagens(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(
        banco, [("10.00", "a"), ("20.00", "b"), ("150.00", "c"), ("150.00", "c"), ("150.00", "c")]
    )
    inserir_lancamentos(sistema, [("10.00", "a"), ("20.00", "b"), ("150.00", "c"), ("150.00", "c")])
    inserir_lancamentos(sistema, [("77.00", "so sistema")])

    response = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert response.status_code == 201
    assert response.json() == {
        "extrato_banco_id": str(banco.id),
        "extrato_sistema_id": str(sistema.id),
        "total": 6,
        "match_exato": 4,
        "duplicado": 1,
        "sem_correspondencia": 1,
    }
    linhas = _conciliacoes(db_session, banco.id, sistema.id)
    assert len(linhas) == 6
    assert all(linha.empresa_id == empresa.id for linha in linhas)
    matches = [linha for linha in linhas if linha.status == "match_exato"]
    assert len(matches) == 4
    for match in matches:
        assert match.regra_aplicada == "exato"
        assert match.score_confianca == Decimal("1.000")
        assert match.lancamento_banco_id is not None
        assert match.lancamento_sistema_id is not None
    for outra in (linha for linha in linhas if linha.status != "match_exato"):
        assert outra.regra_aplicada is None
        assert outra.score_confianca is None
        assert (outra.lancamento_banco_id is None) != (outra.lancamento_sistema_id is None)


def test_post_repetido_e_idempotente(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b")])
    inserir_lancamentos(sistema, [("10.00", "a")])

    primeira = _post(auth_headers(empresa.id), banco.id, sistema.id)
    ids_primeira = {linha.id for linha in _conciliacoes(db_session, banco.id, sistema.id)}
    segunda = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert primeira.status_code == segunda.status_code == 201
    assert primeira.json() == segunda.json()
    linhas = _conciliacoes(db_session, banco.id, sistema.id)
    assert len(linhas) == 2
    assert not ids_primeira & {linha.id for linha in linhas}  # substituídas, não somadas


def test_extrato_de_outra_empresa_retorna_404(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    outra = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")
    sistema = criar_extrato_da_empresa(dona.id, "sistema")

    response = _post(auth_headers(outra.id), banco.id, sistema.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Extrato não encontrado."


def test_extrato_inexistente_retorna_404(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _post(auth_headers(empresa.id), banco.id, uuid.uuid4()).status_code == 404


def test_origem_trocada_retorna_422(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _post(auth_headers(empresa.id), sistema.id, banco.id).status_code == 422


def test_mesmo_id_nos_dois_lados_retorna_422(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _post(auth_headers(empresa.id), banco.id, banco.id).status_code == 422


@pytest.mark.parametrize("status", ["pendente", "processando", "erro"])
def test_extrato_nao_processado_retorna_409(
    status, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", status=status)
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _post(auth_headers(empresa.id), banco.id, sistema.id).status_code == 409


def test_extrato_concluido_com_erros_e_aceito(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", status="concluido_com_erros")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    response = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert response.status_code == 201
    assert response.json()["total"] == 0


def test_sem_token_retorna_401():
    response = client.post(
        "/conciliacoes",
        json={"extrato_banco_id": str(uuid.uuid4()), "extrato_sistema_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401


def test_par_sintetico_via_upload_gera_o_numero_esperado_de_matches(
    db_session, criar_empresa, auth_headers, tmp_path, capsys
):
    quantidade, sobreposicao = 40, 25
    gerar_extratos(
        [
            "--formato", "csv", "--par",
            "--quantidade", str(quantidade),
            "--sobreposicao", str(sobreposicao),
            "--seed", "16",
            "--saida", str(tmp_path),
        ]
    )  # fmt: skip
    esperado = int(re.search(r"matches_esperados=(\d+)", capsys.readouterr().out).group(1))
    assert esperado == sobreposicao

    empresa = criar_empresa()
    ids = {}
    for origem in ("banco", "sistema"):
        arquivo = next(tmp_path.glob(f"extrato_par_{origem}_*.csv"))
        response = client.post(
            "/extratos/upload",
            headers=auth_headers(empresa.id),
            data={"origem": origem},
            files={"arquivo": (arquivo.name, io.BytesIO(arquivo.read_bytes()), "text/csv")},
        )
        assert response.status_code == 201
        ids[origem] = response.json()["extrato_id"]
        db_session.expire_all()
        extrato = db_session.get(Extrato, uuid.UUID(ids[origem]))
        assert extrato.status == "concluido"
        assert extrato.quantidade_lancamentos == quantidade

    response = _post(auth_headers(empresa.id), ids["banco"], ids["sistema"])

    assert response.status_code == 201
    corpo = response.json()
    assert corpo["match_exato"] == esperado
    assert corpo["duplicado"] == 0
    assert corpo["sem_correspondencia"] == 2 * (quantidade - sobreposicao)
    assert corpo["total"] == esperado + 2 * (quantidade - sobreposicao)


def test_duas_conciliacoes_simultaneas_do_mesmo_par_nao_duplicam_linhas(
    db_session,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    monkeypatch,
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    inserir_lancamentos(sistema, [("10.00", "a"), ("20.00", "b")])

    # Ids como valores simples: atributos ORM da sessão principal não podem
    # ser lidos de dentro das threads.
    empresa_id, banco_id, sistema_id = empresa.id, banco.id, sistema.id

    esperado = matching.conciliar_extratos(db_session, empresa_id, banco_id, sistema_id)["total"]
    assert esperado == 3

    # Alarga a janela de corrida entre o delete e o insert: sem o lock, as duas
    # threads apagam o que já existia, esperam e inserem, dobrando as linhas
    # (o delete da segunda não enxerga o que a primeira ainda vai inserir).
    insert_original = matching.insert

    def insert_lento(*args, **kwargs):
        time.sleep(1.5)
        return insert_original(*args, **kwargs)

    monkeypatch.setattr(matching, "insert", insert_lento)

    largada = threading.Barrier(2)
    erros: list[BaseException] = []

    def conciliar_em_sessao_propria():
        sessao = SessionLocal()
        try:
            largada.wait(timeout=30)
            matching.conciliar_extratos(sessao, empresa_id, banco_id, sistema_id)
        except BaseException as exc:  # noqa: BLE001 - reporta no thread principal
            erros.append(exc)
        finally:
            sessao.close()

    threads = [threading.Thread(target=conciliar_em_sessao_propria) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert erros == []
    assert len(_conciliacoes(db_session, banco_id, sistema_id)) == esperado
