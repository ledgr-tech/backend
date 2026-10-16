"""Saldo do extrato (issue #118) nas rotas: `saldo_inicial`, `saldo_final` e
`saldo_data` em cada item de GET /extratos, em GET /extratos/{extrato_id} e
na raiz de GET /conciliacoes/{extrato_id} (o do extrato do banco).

O saldo inicial é calculado na leitura (app/services/saldo.py): saldo final
menos a soma dos lançamentos com data até `saldo_data`, inclusive; nulo sem
saldo final ou com linha não lida.

Precisam de Postgres real (`db_session` do conftest.py) e pulam sem ele.
"""

import hashlib
import io
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.database import engine
from app.models import Lancamento, LinhaInvalida
from main import app

client = TestClient(app)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SEM_SALDO = {"saldo_inicial": None, "saldo_final": None, "saldo_data": None}


def _com_saldo(db_session, extrato, saldo_final: str, saldo_data: date):
    extrato.saldo_final, extrato.saldo_data = Decimal(saldo_final), saldo_data
    db_session.commit()
    return extrato


def _lancar(db_session, extrato, itens: list[tuple[date, str]]) -> None:
    for data_lancamento, valor in itens:
        db_session.add(
            Lancamento(
                empresa_id=extrato.empresa_id,
                extrato_id=extrato.id,
                data=data_lancamento,
                valor=Decimal(valor),
                descricao="x",
                tipo="credito" if Decimal(valor) >= 0 else "debito",
                hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            )
        )
    db_session.commit()


def _saldos_nas_tres_rotas(headers, extrato_id):
    campos = ("saldo_inicial", "saldo_final", "saldo_data")
    detalhe = client.get(f"/extratos/{extrato_id}", headers=headers).json()
    item = next(
        item
        for item in client.get("/extratos", headers=headers).json()["itens"]
        if item["extrato_id"] == str(extrato_id)
    )
    conciliacoes = client.get(f"/conciliacoes/{extrato_id}", headers=headers).json()
    return [{campo: corpo[campo] for campo in campos} for corpo in (detalhe, item, conciliacoes)]


def test_saldo_inicial_desconta_lancamentos_ate_saldo_data_inclusive(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = _com_saldo(
        db_session, criar_extrato_da_empresa(empresa.id, "banco"), "1000.00", date(2026, 9, 10)
    )
    _lancar(
        db_session,
        banco,
        [
            (date(2026, 9, 5), "300.00"),
            (date(2026, 9, 10), "-50.25"),  # no dia do saldo: entra
            (date(2026, 9, 12), "999.00"),  # depois do saldo: não entra
        ],
    )

    saldos = _saldos_nas_tres_rotas(auth_headers(empresa.id), banco.id)

    # 1000.00 - (300.00 - 50.25); Decimal em string, como os outros valores.
    esperado = {"saldo_inicial": "750.25", "saldo_final": "1000.00", "saldo_data": "2026-09-10"}
    assert saldos == [esperado] * 3


def test_saldo_inicial_e_o_final_quando_nao_ha_lancamento_ate_saldo_data(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = _com_saldo(
        db_session, criar_extrato_da_empresa(empresa.id, "banco"), "-80.10", date(2026, 9, 1)
    )
    _lancar(db_session, banco, [(date(2026, 9, 2), "10.00")])

    saldos = _saldos_nas_tres_rotas(auth_headers(empresa.id), banco.id)

    esperado = {"saldo_inicial": "-80.10", "saldo_final": "-80.10", "saldo_data": "2026-09-01"}
    assert saldos == [esperado] * 3


def test_linha_nao_lida_deixa_so_o_saldo_inicial_nulo(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = _com_saldo(
        db_session,
        criar_extrato_da_empresa(empresa.id, "banco", status="concluido_com_erros"),
        "1000.00",
        date(2026, 9, 10),
    )
    _lancar(db_session, banco, [(date(2026, 9, 5), "300.00")])
    db_session.add(LinhaInvalida(extrato_id=banco.id, identificador="3", motivo="x"))
    db_session.commit()

    saldos = _saldos_nas_tres_rotas(auth_headers(empresa.id), banco.id)

    esperado = {"saldo_inicial": None, "saldo_final": "1000.00", "saldo_data": "2026-09-10"}
    assert saldos == [esperado] * 3


def test_extrato_sem_saldo_devolve_os_tres_nulos(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    _lancar(db_session, banco, [(date(2026, 9, 5), "300.00")])

    assert _saldos_nas_tres_rotas(auth_headers(empresa.id), banco.id) == [SEM_SALDO] * 3


def test_upload_de_ofx_com_ledgerbal_devolve_o_saldo(db_session, criar_empresa, auth_headers):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    resposta = client.post(
        "/extratos/upload",
        headers=headers,
        data={"origem": "banco"},
        files={"arquivo": ("extrato.ofx", io.BytesIO(conteudo), "application/x-ofx")},
    )
    assert resposta.status_code == 201
    extrato_id = resposta.json()["extrato_id"]

    saldos = _saldos_nas_tres_rotas(headers, extrato_id)

    # LEDGERBAL 1249.25 em 10/09; lançamentos 1500.00 e -250.75 antes dele.
    esperado = {"saldo_inicial": "0.00", "saldo_final": "1249.25", "saldo_data": "2026-09-10"}
    assert saldos == [esperado] * 3


def test_listagem_soma_os_saldos_numa_consulta_so(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    for indice in range(50):
        extrato = _com_saldo(
            db_session,
            criar_extrato_da_empresa(empresa.id, "banco"),
            f"{indice}.00",
            date(2026, 9, 10),
        )
        _lancar(db_session, extrato, [(date(2026, 9, 5), "1.00"), (date(2026, 9, 11), "7.00")])
        if indice % 5 == 0:
            db_session.add(LinhaInvalida(extrato_id=extrato.id, identificador="2", motivo="x"))
    db_session.commit()

    def selects(limit):
        contados: list[str] = []

        def contar(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                contados.append(statement)

        event.listen(engine, "before_cursor_execute", contar)
        try:
            resposta = client.get("/extratos", headers=headers, params={"limit": limit})
        finally:
            event.remove(engine, "before_cursor_execute", contar)
        itens = resposta.json()["itens"]
        assert len(itens) == limit
        for item in itens:
            saldo_final = Decimal(item["saldo_final"])
            if item["linhas_nao_lidas"]:
                assert item["saldo_inicial"] is None
            else:
                assert Decimal(item["saldo_inicial"]) == saldo_final - Decimal("1.00")
        return len(contados)

    pequena, cheia = selects(5), selects(50)

    # usuário do token, itens, total e a soma agrupada da página: sem N+1
    assert pequena == cheia == 4
