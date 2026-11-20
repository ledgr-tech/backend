"""Saldo do JSON preservado na revisão, confirmação e conciliação (#238)."""

import json
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient

from app.models import Extrato
from app.services.saldo import saldo_do_extrato
from main import app

client = TestClient(app)

LANCAMENTOS = [
    {"data": "2026-09-05", "valor": "1500.00", "descricao": "Recebimento"},
    {"data": "2026-09-06", "valor": "-250.75", "descricao": "Fornecedor"},
    {"data": "2026-09-06", "valor": "-100.00", "descricao": "Tarifa"},
    {"data": "2026-10-01", "valor": "9.00", "descricao": "Depois do saldo"},
]
SALDO = {"saldo_final": "2149.25", "saldo_data": "2026-09-06"}
NULO = {"saldo_final": None, "saldo_data": None}
CSV_BANCO = (
    "Data,Valor,Descricao,Saldo\n"
    "2026-09-05,1500.00,Deposito,1500.00\n"
    "2026-09-06,-250.75,Pagamento,1249.25\n"
)


@pytest.fixture
def conta(db_session, criar_usuario, gerar_token):
    def _nova():
        usuario = criar_usuario(nome="Ana Admin")
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        empresa_id = empresa_unica(db_session, usuario.conta_id)
        return SimpleNamespace(headers=cabecalhos(token, empresa_id))

    return _nova


def _importar(headers, saldo=SALDO):
    conteudo = json.dumps({"versao": "1", "lancamentos": LANCAMENTOS, **saldo}).encode()
    resposta = client.post(
        "/importacoes-ia",
        headers=headers,
        files={"arquivo": ("relatorio.json", conteudo, "application/json")},
    )
    assert resposta.status_code == 201, resposta.text
    return resposta.json()


def _revisar(headers, importacao_id):
    resposta = client.get(f"/importacoes-ia/{importacao_id}", headers=headers)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def _corrigir(headers, importacao_id, lancamento_id, **campos):
    return client.patch(
        f"/importacoes-ia/{importacao_id}/lancamentos/{lancamento_id}",
        headers=headers,
        json=campos,
    )


def _confirmar(headers, importacao_id):
    resposta = client.post(f"/importacoes-ia/{importacao_id}/confirmar", headers=headers)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def _conciliar(headers, banco_id, sistema_id):
    resposta = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": banco_id, "extrato_sistema_id": sistema_id},
    )
    assert resposta.status_code == 201, resposta.text


def _get(headers, extrato_id):
    resposta = client.get(f"/conciliacoes/{extrato_id}", headers=headers)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def _saldos(corpo):
    return {
        campo: corpo[campo]
        for campo in (
            "saldo_inicial",
            "saldo_final",
            "saldo_data",
            "saldo_sistema",
            "diferenca_saldo_final",
            "diferenca_saldo_motivo",
        )
    }


def _saldo_informado(corpo):
    return {campo: corpo[campo] for campo in NULO}


@pytest.mark.parametrize("saldo", [SALDO, {}], ids=["com_saldo", "sem_saldo"])
def test_importacao_grava_e_devolve_saldo_no_201_e_na_revisao(db_session, conta, saldo):
    c = conta()
    esperado = saldo or NULO

    corpo = _importar(c.headers, saldo)
    revisao = _revisar(c.headers, corpo["importacao_id"])

    assert _saldo_informado(corpo) == esperado
    assert _saldo_informado(revisao["resumo"]) == esperado
    db_session.expire_all()
    extrato = db_session.get(Extrato, uuid.UUID(corpo["importacao_id"]))
    assert extrato.saldo_final == (Decimal(saldo["saldo_final"]) if saldo else None)
    assert extrato.saldo_data == (date(2026, 9, 6) if saldo else None)


@pytest.mark.parametrize("saldo", [SALDO, {}], ids=["com_saldo", "sem_saldo"])
def test_patch_e_post_de_lancamento_preservam_saldo_no_resumo(db_session, conta, saldo):
    c = conta()
    importacao_id = _importar(c.headers, saldo)["importacao_id"]
    primeira = _revisar(c.headers, importacao_id)["itens"][0]["id"]

    corrigida = _corrigir(c.headers, importacao_id, primeira, valor="1600.00")
    adicionada = client.post(
        f"/importacoes-ia/{importacao_id}/lancamentos",
        headers=c.headers,
        json={"data": "2026-09-06", "valor": "-10.00", "descricao": "Ajuste"},
    )

    assert corrigida.status_code == 200, corrigida.text
    assert adicionada.status_code == 201, adicionada.text
    esperado = saldo or NULO
    assert _saldo_informado(corrigida.json()["resumo"]) == esperado
    assert _saldo_informado(adicionada.json()["resumo"]) == esperado
    assert _saldo_informado(_revisar(c.headers, importacao_id)["resumo"]) == esperado


@pytest.mark.parametrize("saldo", [SALDO, {}], ids=["com_saldo", "sem_saldo"])
def test_confirmar_preserva_saldo_e_devolve_inicial_calculado(db_session, conta, saldo):
    c = conta()
    importacao_id = _importar(c.headers, saldo)["importacao_id"]

    corpo = _confirmar(c.headers, importacao_id)

    assert _saldo_informado(corpo) == (saldo or NULO)
    # 2149.25 - (1500 - 250.75 - 100) = 1000; a linha de outubro não entra.
    assert corpo["saldo_inicial"] == ("1000.00" if saldo else None)
    detalhe = client.get(f"/extratos/{importacao_id}", headers=c.headers)
    assert detalhe.status_code == 200, detalhe.text
    assert _saldo_informado(detalhe.json()) == (saldo or NULO)
    assert detalhe.json()["saldo_inicial"] == corpo["saldo_inicial"]
    db_session.expire_all()
    extrato = db_session.get(Extrato, uuid.UUID(importacao_id))
    assert extrato.status == "concluido"
    assert extrato.saldo_final == (Decimal("2149.25") if saldo else None)
    assert extrato.saldo_data == (date(2026, 9, 6) if saldo else None)


def test_corrigir_valor_muda_saldo_inicial_sem_mudar_final(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)["importacao_id"]
    primeira = _revisar(c.headers, importacao_id)["itens"][0]["id"]
    extrato = db_session.get(Extrato, uuid.UUID(importacao_id))
    antes = saldo_do_extrato(db_session, extrato)
    assert antes.saldo_inicial == Decimal("1000.00")

    resposta = _corrigir(c.headers, importacao_id, primeira, valor="1600.00")

    assert resposta.status_code == 200, resposta.text
    db_session.expire_all()
    depois = saldo_do_extrato(db_session, extrato)
    assert depois.saldo_inicial == Decimal("900.00")
    assert depois.saldo_final == antes.saldo_final == Decimal("2149.25")
    assert depois.saldo_data == antes.saldo_data == date(2026, 9, 6)
    confirmado = _confirmar(c.headers, importacao_id)
    assert confirmado["saldo_inicial"] == "900.00"
    assert _saldo_informado(confirmado) == SALDO


@pytest.mark.parametrize("saldo", [SALDO, {}], ids=["com_saldo", "sem_saldo"])
def test_json_confirmado_leva_saldo_a_conciliacao(db_session, conta, saldo):
    c = conta()
    upload = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "banco"},
        files={"arquivo": ("banco.csv", CSV_BANCO.encode(), "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    banco_id = upload.json()["extrato_id"]
    importacao_id = _importar(c.headers, saldo)["importacao_id"]
    _confirmar(c.headers, importacao_id)

    _conciliar(c.headers, banco_id, importacao_id)
    corpo = _get(c.headers, banco_id)

    assert _saldos(corpo) == {
        "saldo_inicial": "0.00",
        "saldo_final": "1249.25",
        "saldo_data": "2026-09-06",
        "saldo_sistema": {"saldo_inicial": "1000.00" if saldo else None, **(saldo or NULO)},
        "diferenca_saldo_final": "-900.00" if saldo else None,
        "diferenca_saldo_motivo": None if saldo else "sem_saldo_sistema",
    }
