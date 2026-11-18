"""Testes do `GET /busca` (issue #220): a busca do topo do app (Ctrl K) nos
lançamentos de todas as conciliações da empresa ativa.

A interpretação do termo (`interpretar_termo`) é pura e testada sem banco. A
rota roda contra o Postgres real, montando conciliações pelo `POST
/conciliacoes`, o mesmo caminho do app.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.models import Extrato, Lancamento
from app.services.busca import DataBuscada, interpretar_termo
from main import app

client = TestClient(app)


# --- interpretação do termo --------------------------------------------------


@pytest.mark.parametrize(
    ("termo", "valores"),
    [
        ("1.234,56", {Decimal("1234.56")}),
        ("1234,56", {Decimal("1234.56")}),
        ("1234.56", {Decimal("1234.56")}),
        ("1,234.56", {Decimal("1234.56")}),
        ("R$ 1.234,56", {Decimal("1234.56")}),
        ("-150,00", {Decimal("150.00")}),
        ("1.234", {Decimal(1234)}),
        ("12.5", {Decimal("12.5")}),
        ("150", {Decimal(150)}),
        ("0,99", {Decimal("0.99")}),
    ],
)
def test_termo_com_formato_de_valor_vira_valor(termo, valores):
    assert interpretar_termo(termo).valores == valores


@pytest.mark.parametrize("termo", ["Tarifa", "12,345", "1.2.3", "13/10", "1234567890123", ""])
def test_termo_sem_formato_de_valor_nao_vira_valor(termo):
    assert interpretar_termo(termo).valores == set()


@pytest.mark.parametrize(
    ("termo", "data"),
    [
        ("13/10", DataBuscada(dia=13, mes=10, ano=None)),
        ("5/9", DataBuscada(dia=5, mes=9, ano=None)),
        ("13/10/2026", DataBuscada(dia=13, mes=10, ano=2026)),
        ("13/10/26", DataBuscada(dia=13, mes=10, ano=2026)),
        ("2026-10-13", DataBuscada(dia=13, mes=10, ano=2026)),
    ],
)
def test_termo_com_formato_de_data_vira_data(termo, data):
    assert interpretar_termo(termo).datas == [data]


@pytest.mark.parametrize("termo", ["32/10", "13/13", "31/02/2026", "13-10", "1234,56", "Pix"])
def test_termo_sem_data_valida_nao_vira_data(termo):
    assert interpretar_termo(termo).datas == []


def test_texto_sai_sem_acento_e_em_minusculas():
    assert interpretar_termo("  Pagamento JOÃO  ").texto == "pagamento joao"


def test_texto_de_um_caractere_nao_entra_na_busca():
    termo = interpretar_termo("5")
    assert termo.texto is None
    assert termo.valores == {Decimal(5)}


def test_termo_vazio_nao_busca_nada():
    assert interpretar_termo("   ").vazio


def test_termo_de_uma_palavra_nao_tem_palavras_soltas():
    assert interpretar_termo("joão").palavras == ()


def test_varias_palavras_viram_palavras_soltas():
    termo = interpretar_termo("  Pix   JOÃO 150 13/10 ")
    assert termo.texto == "pix   joao 150 13/10"
    assert [palavra.texto for palavra in termo.palavras] == ["pix", "joao", "150", "13/10"]
    assert termo.palavras[2].valores == {Decimal(150)}
    assert termo.palavras[3].datas == [DataBuscada(dia=13, mes=10, ano=None)]


def test_palavra_de_um_caractere_que_nao_e_valor_fica_de_fora():
    termo = interpretar_termo("joão e 5")
    assert [palavra.texto for palavra in termo.palavras] == ["joao", None]
    assert termo.palavras[1].valores == {Decimal(5)}


# --- rota -------------------------------------------------------------------


def _buscar(headers, q, **params):
    return client.get("/busca", headers=headers, params={"q": q, **params})


def _conciliar(headers, banco, sistema):
    resposta = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert resposta.status_code == 201, resposta.text
    return resposta


@pytest.fixture
def empresa(db_session, criar_empresa):
    return criar_empresa()


@pytest.fixture
def par(db_session, empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers):
    """Um par conciliado: banco e sistema com um lançamento que casa, um só do
    banco e um só do sistema. O extrato do banco é de setembro de 2026."""

    def _criar(itens_banco, itens_sistema, periodo_inicio=date(2026, 9, 1)):
        banco = criar_extrato_da_empresa(empresa.id, "banco")
        sistema = criar_extrato_da_empresa(empresa.id, "sistema")
        db_session.execute(
            update(Extrato)
            .where(Extrato.id == banco.id)
            .values(periodo_inicio=periodo_inicio, periodo_fim=periodo_inicio + timedelta(days=27))
        )
        db_session.commit()
        inserir_lancamentos(banco, itens_banco)
        inserir_lancamentos(sistema, itens_sistema)
        _conciliar(auth_headers(empresa.id), banco, sistema)
        return banco, sistema

    return _criar


def test_busca_texto_sem_acento_nem_maiusculas_em_qualquer_lado(par, empresa, auth_headers):
    par(
        [("10.00", "PIX RECEBIDO JOÃO SILVA"), ("20.00", "Tarifa pacote")],
        [("10.00", "Recebimento cliente"), ("99.00", "Fornecedor Gráfica Ágil")],
    )

    resposta = _buscar(auth_headers(empresa.id), "joao")
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["total"] == 1
    item = corpo["itens"][0]
    assert item["lancamento_banco"]["descricao"] == "PIX RECEBIDO JOÃO SILVA"
    assert item["lancamento_sistema"]["descricao"] == "Recebimento cliente"

    do_sistema = _buscar(auth_headers(empresa.id), "GRAFICA AGIL").json()
    assert do_sistema["total"] == 1
    assert do_sistema["itens"][0]["lancamento_banco"] is None
    assert do_sistema["itens"][0]["lancamento_sistema"]["descricao"] == "Fornecedor Gráfica Ágil"


def test_item_traz_o_que_o_front_precisa_para_abrir_a_linha(par, empresa, auth_headers):
    banco, sistema = par([("10.00", "Pix único")], [("10.00", "Pix único")])
    headers = auth_headers(empresa.id)

    item = _buscar(headers, "pix único").json()["itens"][0]

    listagem = client.get(f"/conciliacoes/{banco.id}", headers=headers).json()["itens"]
    assert len(listagem) == 1
    assert item == {
        "conciliacao_id": listagem[0]["id"],
        "extrato_banco_id": str(banco.id),
        "extrato_sistema_id": str(sistema.id),
        "chave": listagem[0]["chave"],
        "status": "match_exato",
        "competencia": "2026-09",
        "lancamento_banco": listagem[0]["lancamento_banco"],
        "lancamento_sistema": listagem[0]["lancamento_sistema"],
    }


def test_busca_valor_em_formato_brasileiro_acha_credito_e_debito(par, empresa, auth_headers):
    par(
        [("1234.56", "Credito"), ("-1234.56", "Debito"), ("1234.50", "Outro")],
        [("77.00", "Nada")],
    )

    corpo = _buscar(auth_headers(empresa.id), "1.234,56").json()

    descricoes = sorted(i["lancamento_banco"]["descricao"] for i in corpo["itens"])
    assert descricoes == ["Credito", "Debito"]


def test_busca_data_com_e_sem_ano(db_session, par, empresa, auth_headers):
    banco, _ = par([("10.00", "Dia cinco"), ("20.00", "Dia seis")], [("77.00", "Nada")])
    db_session.execute(
        update(Lancamento)
        .where(Lancamento.extrato_id == banco.id, Lancamento.descricao == "Dia seis")
        .values(data=date(2026, 9, 6))
    )
    db_session.commit()
    headers = auth_headers(empresa.id)

    def _descricoes(q):
        return [
            i["lancamento_banco"]["descricao"]
            for i in _buscar(headers, q).json()["itens"]
            if i["lancamento_banco"]
        ]

    assert _descricoes("05/09") == ["Dia cinco"]
    assert _descricoes("05/09/2026") == ["Dia cinco"]
    assert _descricoes("05/09/2025") == []
    assert _descricoes("2026-09-06") == ["Dia seis"]


def test_busca_em_todas_as_conciliacoes_da_empresa(par, empresa, auth_headers):
    banco_setembro, _ = par([("10.00", "Aluguel setembro")], [("77.00", "Nada")])
    banco_outubro, _ = par(
        [("10.00", "Aluguel outubro")], [("88.00", "Nada")], periodo_inicio=date(2026, 10, 1)
    )

    corpo = _buscar(auth_headers(empresa.id), "aluguel").json()

    assert corpo["total"] == 2
    assert {(i["extrato_banco_id"], i["competencia"]) for i in corpo["itens"]} == {
        (str(banco_setembro.id), "2026-09"),
        (str(banco_outubro.id), "2026-10"),
    }


def test_extrato_nao_conciliado_fica_fora(
    par, empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    par([("10.00", "Energia conciliada")], [("77.00", "Nada")])
    solto = criar_extrato_da_empresa(empresa.id, "banco")
    inserir_lancamentos(solto, [("10.00", "Energia sem conciliação")])

    corpo = _buscar(auth_headers(empresa.id), "energia").json()

    assert [i["lancamento_banco"]["descricao"] for i in corpo["itens"]] == ["Energia conciliada"]


def test_so_o_par_em_vigor_de_cada_extrato_do_banco(
    db_session, empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    """Conciliado com o sistema A e depois com o B, o extrato do banco aparece
    só pelo par com o B, o mesmo par em que as decisões são gravadas."""
    headers = auth_headers(empresa.id)
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema_a = criar_extrato_da_empresa(empresa.id, "sistema")
    sistema_b = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "Condominio")])
    inserir_lancamentos(sistema_a, [("10.00", "Condominio"), ("55.00", "Condominio extra A")])
    inserir_lancamentos(sistema_b, [("10.00", "Condominio")])
    _conciliar(headers, banco, sistema_a)
    _conciliar(headers, banco, sistema_b)

    corpo = _buscar(headers, "condominio").json()

    assert corpo["total"] == 1
    assert corpo["itens"][0]["extrato_sistema_id"] == str(sistema_b.id)


def test_outra_empresa_da_mesma_conta_nao_aparece(
    db_session, par, empresa, criar_empresa, auth_headers
):
    par([("10.00", "Internet")], [("77.00", "Nada")])
    outra = criar_empresa(conta_id=empresa.conta_id)

    corpo = _buscar(auth_headers(outra.id), "internet").json()

    assert corpo == {"total": 0, "limit": 10, "offset": 0, "itens": []}


def test_pagina_com_os_mais_recentes_primeiro(db_session, par, empresa, auth_headers):
    banco, _ = par([(f"{v}.00", f"Boleto {v}") for v in (10, 20, 30)], [("77.00", "Nada")])
    for dia, valor in ((1, "10.00"), (3, "20.00"), (2, "30.00")):
        db_session.execute(
            update(Lancamento)
            .where(Lancamento.extrato_id == banco.id, Lancamento.valor == Decimal(valor))
            .values(data=date(2026, 9, dia))
        )
    db_session.commit()
    headers = auth_headers(empresa.id)

    primeira = _buscar(headers, "boleto", limit=2).json()
    segunda = _buscar(headers, "boleto", limit=2, offset=2).json()
    alem = _buscar(headers, "boleto", limit=2, offset=10).json()

    assert (primeira["total"], segunda["total"], alem["total"]) == (3, 3, 3)
    assert [i["lancamento_banco"]["descricao"] for i in primeira["itens"]] == [
        "Boleto 20",
        "Boleto 30",
    ]
    assert [i["lancamento_banco"]["descricao"] for i in segunda["itens"]] == ["Boleto 10"]
    assert alem["itens"] == []


def test_palavras_soltas_em_qualquer_ordem_no_mesmo_lancamento(par, empresa, auth_headers):
    par(
        [("10.00", "PIX RECEBIDO JOÃO SILVA"), ("20.00", "Pix enviado Maria")],
        [("10.00", "Recebimento cliente"), ("30.00", "João transferência")],
    )
    headers = auth_headers(empresa.id)

    def descricoes(q):
        corpo = _buscar(headers, q).json()
        return sorted(
            (item["lancamento_banco"] or item["lancamento_sistema"])["descricao"]
            for item in corpo["itens"]
        )

    assert descricoes("joao pix") == ["PIX RECEBIDO JOÃO SILVA"]
    assert descricoes("silva  RECEBIDO") == ["PIX RECEBIDO JOÃO SILVA"]
    assert descricoes("joao e silva") == ["PIX RECEBIDO JOÃO SILVA"]
    # As palavras precisam estar no mesmo lançamento: "pix" está no banco e
    # "cliente" no sistema da mesma linha, e a linha não entra.
    assert descricoes("pix cliente") == []
    assert descricoes("joao inexistente") == []


def test_palavras_soltas_combinam_texto_valor_e_data(db_session, par, empresa, auth_headers):
    banco, _ = par(
        [("150.00", "Pix João"), ("150.00", "Pix Maria"), ("80.00", "Pix João")],
        [("999.00", "Nada")],
    )
    db_session.execute(
        update(Lancamento)
        .where(Lancamento.extrato_id == banco.id, Lancamento.descricao == "Pix Maria")
        .values(data=date(2026, 10, 13))
    )
    db_session.commit()
    headers = auth_headers(empresa.id)

    def achados(q):
        return sorted(
            (i["lancamento_banco"]["descricao"], i["lancamento_banco"]["valor"])
            for i in _buscar(headers, q).json()["itens"]
        )

    assert achados("joao 150") == [("Pix João", "150.00")]
    assert achados("150,00 pix") == [("Pix João", "150.00"), ("Pix Maria", "150.00")]
    assert achados("pix 13/10") == [("Pix Maria", "150.00")]
    # O termo inteiro ainda vale como valor: "R$ 150" não exige "r$" na descrição.
    assert len(achados("R$ 150")) == 2


def test_curinga_do_like_e_texto_comum(par, empresa, auth_headers):
    par([("10.00", "Desconto 10% à vista"), ("20.00", "Sem desconto")], [("77.00", "Nada")])
    headers = auth_headers(empresa.id)

    assert _buscar(headers, "%%").json()["total"] == 0
    assert _buscar(headers, "10%").json()["total"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},
        {"q": "   "},
        {"q": "x" * 101},
        {},
        {"q": "pix", "limit": 0},
        {"q": "pix", "limit": 51},
    ],
)
def test_parametros_invalidos_dao_422(empresa, auth_headers, params):
    resposta = client.get("/busca", headers=auth_headers(empresa.id), params=params)
    assert resposta.status_code == 422


def test_sem_token_e_401():
    assert client.get("/busca", params={"q": "pix"}).status_code == 401


def test_empresa_de_outra_conta_no_cabecalho_nao_vaza(
    db_session, par, empresa, criar_empresa, auth_headers
):
    par([("10.00", "Segredo da empresa")], [("77.00", "Nada")])
    intrusa = criar_empresa()
    headers = auth_headers(intrusa.id)
    headers["X-Ledgr-Empresa"] = str(empresa.id)

    resposta = _buscar(headers, "segredo")

    assert resposta.status_code in (403, 404)
    assert "Segredo" not in resposta.text


def test_id_aleatorio_nao_quebra(empresa, auth_headers):
    resposta = _buscar(auth_headers(empresa.id), str(uuid.uuid4()))
    assert resposta.status_code == 200
    assert resposta.json()["total"] == 0
