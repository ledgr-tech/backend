"""Saldo do extrato do sistema na comparação (issue #238, parte 1): na raiz de
GET /conciliacoes/{extrato_id}, `saldo_sistema` (os três campos do saldo do
extrato do sistema da rodada exibida), `diferenca_saldo_final` e
`diferenca_saldo_motivo`, ao lado do saldo do banco da issue #118, que não
muda.

Regras em app/services/saldo.py: `diferenca_dos_saldos` é função pura e é
testada sem banco; as rotas precisam de Postgres real (`db_session` do
conftest.py) e pulam sem ele.
"""

import hashlib
import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.database import engine
from app.models import Lancamento, LinhaInvalida
from app.services import saldo
from app.services.saldo import SEM_SALDO, DiferencaDeSaldo, Saldo, diferenca_dos_saldos
from main import app

client = TestClient(app)

NULO = {"saldo_inicial": None, "saldo_final": None, "saldo_data": None}
CAMPOS_DO_BANCO = ("saldo_inicial", "saldo_final", "saldo_data")


# Função pura: diferenca_dos_saldos


def _saldo(final: str, data_saldo: date, inicial: str | None = "0.00") -> Saldo:
    return Saldo(
        saldo_inicial=None if inicial is None else Decimal(inicial),
        saldo_final=Decimal(final),
        saldo_data=data_saldo,
    )


@pytest.mark.parametrize(
    ("banco", "sistema", "esperado"),
    [
        ("1000.00", "750.25", Decimal("249.75")),
        ("-80.10", "19.90", Decimal("-100.00")),
        ("500.00", "500.00", Decimal("0.00")),
    ],
)
def test_diferenca_com_os_dois_saldos_na_mesma_data(banco, sistema, esperado):
    dia = date(2026, 9, 30)

    diferenca = diferenca_dos_saldos(_saldo(banco, dia), _saldo(sistema, dia))

    assert diferenca == DiferencaDeSaldo(valor=esperado, motivo=None)
    assert str(diferenca.valor) == str(esperado)  # duas casas, como no JSON


def test_diferenca_nao_depende_do_saldo_inicial():
    # Linha não lida no sistema deixa só o inicial nulo: a diferença sai.
    dia = date(2026, 9, 30)

    diferenca = diferenca_dos_saldos(_saldo("10.00", dia), _saldo("4.00", dia, inicial=None))

    assert diferenca == DiferencaDeSaldo(valor=Decimal("6.00"), motivo=None)


def test_diferenca_com_datas_diferentes_e_nula():
    diferenca = diferenca_dos_saldos(
        _saldo("10.00", date(2026, 9, 30)), _saldo("10.00", date(2026, 9, 29))
    )

    assert diferenca == DiferencaDeSaldo(valor=None, motivo="datas_diferentes")


def test_diferenca_sem_saldo_no_banco():
    diferenca = diferenca_dos_saldos(SEM_SALDO, _saldo("10.00", date(2026, 9, 30)))

    assert diferenca == DiferencaDeSaldo(valor=None, motivo="sem_saldo_banco")


def test_diferenca_sem_saldo_no_sistema():
    diferenca = diferenca_dos_saldos(_saldo("10.00", date(2026, 9, 30)), SEM_SALDO)

    assert diferenca == DiferencaDeSaldo(valor=None, motivo="sem_saldo_sistema")


def test_diferenca_sem_nenhum_saldo_o_motivo_e_o_do_banco():
    assert diferenca_dos_saldos(SEM_SALDO, SEM_SALDO) == DiferencaDeSaldo(
        valor=None, motivo="sem_saldo_banco"
    )


# Rota: GET /conciliacoes/{extrato_id}


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


def _conciliar(headers, banco_id, sistema_id) -> None:
    resposta = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )
    assert resposta.status_code == 201, resposta.text


def _get(headers, extrato_id, **params):
    resposta = client.get(f"/conciliacoes/{extrato_id}", headers=headers, params=params)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def _saldos(corpo) -> dict:
    """Só os campos de saldo da raiz: os do banco (#118) e os da #238."""
    return {
        campo: corpo[campo]
        for campo in (
            *CAMPOS_DO_BANCO,
            "saldo_sistema",
            "diferenca_saldo_final",
            "diferenca_saldo_motivo",
        )
    }


@pytest.fixture
def par(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    """Extrato do banco e do sistema da mesma empresa, com lançamentos até
    30/09 e um depois, ainda sem saldo e sem conciliação."""

    def _criar():
        empresa = criar_empresa()
        banco = criar_extrato_da_empresa(empresa.id, "banco")
        sistema = criar_extrato_da_empresa(empresa.id, "sistema")
        _lancar(db_session, banco, [(date(2026, 9, 5), "300.00"), (date(2026, 10, 1), "9.00")])
        _lancar(db_session, sistema, [(date(2026, 9, 5), "300.00"), (date(2026, 9, 30), "-0.50")])
        return empresa, banco, sistema, auth_headers(empresa.id)

    return _criar


def test_par_com_os_dois_saldos_vindos_do_csv(criar_empresa, auth_headers):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    arquivos = {
        "banco": (
            "Data,Valor,Descricao,Saldo\n"
            "2026-09-05,1500.00,Deposito,1500.00\n"
            "2026-09-06,-250.75,Pagamento,1249.25\n"
        ),
        "sistema": (
            "Data,Valor,Descricao,Saldo\n"
            "2026-09-05,1500.00,Recebimento,2500.00\n"
            "2026-09-06,-250.75,Fornecedor,2249.25\n"
            "2026-09-06,-100.00,Tarifa,2149.25\n"
        ),
    }
    ids = {}
    for origem, conteudo in arquivos.items():
        resposta = client.post(
            "/extratos/upload",
            headers=headers,
            data={"origem": origem},
            files={"arquivo": (f"{origem}.csv", io.BytesIO(conteudo.encode()), "text/csv")},
        )
        assert resposta.status_code == 201, resposta.text
        ids[origem] = resposta.json()["extrato_id"]
    _conciliar(headers, ids["banco"], ids["sistema"])

    corpo = _get(headers, ids["banco"])

    assert _saldos(corpo) == {
        "saldo_inicial": "0.00",
        "saldo_final": "1249.25",
        "saldo_data": "2026-09-06",
        "saldo_sistema": {
            "saldo_inicial": "1000.00",
            "saldo_final": "2149.25",
            "saldo_data": "2026-09-06",
        },
        "diferenca_saldo_final": "-900.00",
        "diferenca_saldo_motivo": None,
    }


def test_so_o_banco_com_saldo(db_session, par):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    _conciliar(headers, banco.id, sistema.id)

    assert _saldos(_get(headers, banco.id)) == {
        "saldo_inicial": "700.00",
        "saldo_final": "1000.00",
        "saldo_data": "2026-09-30",
        "saldo_sistema": NULO,
        "diferenca_saldo_final": None,
        "diferenca_saldo_motivo": "sem_saldo_sistema",
    }


def test_so_o_sistema_com_saldo(db_session, par):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, sistema, "1000.00", date(2026, 9, 30))
    _conciliar(headers, banco.id, sistema.id)

    assert _saldos(_get(headers, banco.id)) == {
        **NULO,
        "saldo_sistema": {
            "saldo_inicial": "700.50",
            "saldo_final": "1000.00",
            "saldo_data": "2026-09-30",
        },
        "diferenca_saldo_final": None,
        "diferenca_saldo_motivo": "sem_saldo_banco",
    }


def test_nenhum_dos_dois_com_saldo(par):
    _, banco, sistema, headers = par()
    _conciliar(headers, banco.id, sistema.id)

    assert _saldos(_get(headers, banco.id)) == {
        **NULO,
        "saldo_sistema": NULO,
        "diferenca_saldo_final": None,
        "diferenca_saldo_motivo": "sem_saldo_banco",
    }


def test_os_dois_com_saldo_em_datas_diferentes(db_session, par):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    _com_saldo(db_session, sistema, "1000.00", date(2026, 9, 29))
    _conciliar(headers, banco.id, sistema.id)

    corpo = _get(headers, banco.id)

    assert corpo["saldo_sistema"] == {
        "saldo_inicial": "700.00",
        "saldo_final": "1000.00",
        "saldo_data": "2026-09-29",
    }
    assert corpo["diferenca_saldo_final"] is None
    assert corpo["diferenca_saldo_motivo"] == "datas_diferentes"


def test_sistema_com_linha_nao_lida_deixa_so_o_inicial_nulo(db_session, par):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    sistema.status = "concluido_com_erros"
    _com_saldo(db_session, sistema, "990.00", date(2026, 9, 30))
    db_session.add(LinhaInvalida(extrato_id=sistema.id, identificador="4", motivo="x"))
    db_session.commit()
    _conciliar(headers, banco.id, sistema.id)

    corpo = _get(headers, banco.id)

    assert corpo["saldo_sistema"] == {
        "saldo_inicial": None,
        "saldo_final": "990.00",
        "saldo_data": "2026-09-30",
    }
    assert (corpo["diferenca_saldo_final"], corpo["diferenca_saldo_motivo"]) == ("10.00", None)
    # A linha não lida é do sistema: o inicial do banco continua calculado.
    assert corpo["saldo_inicial"] == "700.00"


def test_filtro_escolhe_o_saldo_da_rodada_antiga_e_sem_filtro_vale_a_mais_recente(
    db_session, par, criar_extrato_da_empresa
):
    empresa, banco, v1, headers = par()
    v2 = criar_extrato_da_empresa(empresa.id, "sistema")
    _lancar(db_session, v2, [(date(2026, 9, 5), "300.00")])
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    _com_saldo(db_session, v1, "900.00", date(2026, 9, 30))
    _com_saldo(db_session, v2, "950.00", date(2026, 9, 30))
    _conciliar(headers, banco.id, v1.id)
    _conciliar(headers, banco.id, v2.id)
    # Reconciliar a v1 depois não a torna a rodada mais recente (decisoes.py).
    _conciliar(headers, banco.id, v1.id)

    antiga = _get(headers, banco.id, extrato_sistema_id=str(v1.id))
    recente = _get(headers, banco.id)

    assert antiga["saldo_sistema"]["saldo_final"] == "900.00"
    assert antiga["saldo_sistema"]["saldo_inicial"] == "600.50"
    assert antiga["diferenca_saldo_final"] == "100.00"
    assert {item["extrato_sistema_id"] for item in antiga["itens"]} == {str(v1.id)}
    assert recente["saldo_sistema"] == {
        "saldo_inicial": "650.00",
        "saldo_final": "950.00",
        "saldo_data": "2026-09-30",
    }
    assert recente["diferenca_saldo_final"] == "50.00"
    assert recente["diferenca_saldo_motivo"] is None
    # O filtro com a rodada atual dá o mesmo saldo de sem filtro.
    filtrada = _get(headers, banco.id, extrato_sistema_id=str(v2.id))
    assert _saldos(filtrada) == _saldos(recente)


@pytest.mark.parametrize(
    ("saldo_banco", "motivo"),
    [(("1000.00", date(2026, 9, 30)), "sem_saldo_sistema"), (None, "sem_saldo_banco")],
)
def test_extrato_do_banco_sem_conciliacao(db_session, par, saldo_banco, motivo):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, sistema, "990.00", date(2026, 9, 30))
    if saldo_banco is not None:
        _com_saldo(db_session, banco, *saldo_banco)

    corpo = _get(headers, banco.id)

    assert corpo["itens"] == [] and corpo["total"] == 0
    assert corpo["saldo_sistema"] == NULO
    assert corpo["diferenca_saldo_final"] is None
    assert corpo["diferenca_saldo_motivo"] == motivo


def test_filtro_com_extrato_que_nao_e_do_par_traz_saldo_nulo_e_lista_vazia(
    db_session, par, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    """Comportamento do filtro de hoje (200 e lista vazia, sem erro novo); o
    saldo do sistema só sai de um extrato já conciliado com este banco."""
    empresa, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    _com_saldo(db_session, sistema, "990.00", date(2026, 9, 30))
    _conciliar(headers, banco.id, sistema.id)
    nao_conciliado = _com_saldo(
        db_session,
        criar_extrato_da_empresa(empresa.id, "sistema"),
        "500.00",
        date(2026, 9, 30),
    )
    outra_empresa = criar_empresa()
    de_outra_empresa = _com_saldo(
        db_session,
        criar_extrato_da_empresa(outra_empresa.id, "sistema"),
        "500.00",
        date(2026, 9, 30),
    )

    for filtro in (nao_conciliado.id, de_outra_empresa.id, uuid.uuid4(), banco.id):
        corpo = _get(headers, banco.id, extrato_sistema_id=str(filtro))

        assert corpo["itens"] == [] and corpo["total"] == 0
        assert corpo["saldo_sistema"] == NULO
        assert corpo["diferenca_saldo_motivo"] == "sem_saldo_sistema"
        assert corpo["saldo_final"] == "1000.00"


def test_campos_do_banco_na_raiz_sao_os_mesmos_da_118(db_session, par):
    """Regressão da #118: os três campos do banco continuam com o mesmo nome e
    os mesmos valores de GET /extratos/{id}, com ou sem saldo do sistema."""
    _, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))

    def do_banco(corpo):
        return {campo: corpo[campo] for campo in CAMPOS_DO_BANCO}

    detalhe = client.get(f"/extratos/{banco.id}", headers=headers).json()
    esperado = {"saldo_inicial": "700.00", "saldo_final": "1000.00", "saldo_data": "2026-09-30"}
    assert do_banco(detalhe) == esperado

    assert do_banco(_get(headers, banco.id)) == esperado
    _conciliar(headers, banco.id, sistema.id)
    assert do_banco(_get(headers, banco.id)) == esperado
    _com_saldo(db_session, sistema, "1.00", date(2026, 9, 30))
    assert do_banco(_get(headers, banco.id)) == esperado
    assert do_banco(_get(headers, banco.id, extrato_sistema_id=str(sistema.id))) == esperado


def test_saldo_do_sistema_custa_no_maximo_uma_consulta(db_session, par, monkeypatch):
    _, banco, sistema, headers = par()
    _com_saldo(db_session, banco, "1000.00", date(2026, 9, 30))
    _com_saldo(db_session, sistema, "990.00", date(2026, 9, 30))
    _lancar(db_session, banco, [(date(2026, 9, 6), f"{indice}.00") for indice in range(1, 31)])
    _conciliar(headers, banco.id, sistema.id)

    def selects(limit, **params):
        contados: list[str] = []

        def contar(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                contados.append(statement)

        _get(headers, banco.id, limit=limit, **params)  # aquece a sessão do token
        event.listen(engine, "before_cursor_execute", contar)
        try:
            corpo = _get(headers, banco.id, limit=limit, **params)
        finally:
            event.remove(engine, "before_cursor_execute", contar)
        assert len(corpo["itens"]) == limit
        return len(contados)

    com_saldo = [selects(5), selects(30), selects(5, extrato_sistema_id=str(sistema.id))]

    # A mesma rota sem a consulta do saldo do sistema: o custo de hoje.
    monkeypatch.setattr(saldo, "saldo_do_extrato_do_sistema", lambda *_: SEM_SALDO)
    sem_saldo = [selects(5), selects(30), selects(5, extrato_sistema_id=str(sistema.id))]

    # Nenhuma por item, e uma só a mais: a rodada mais recente já era lida
    # para `aguardando_aprovacao` e é reaproveitada.
    assert com_saldo[0] == com_saldo[1]
    assert [com - sem for com, sem in zip(com_saldo, sem_saldo, strict=True)] == [1, 1, 1]
