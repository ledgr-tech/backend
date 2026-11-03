"""Painel administrativo, parte 2 (issue #169, ADR-025 e ADR-026): pagamentos,
cancelamentos, vencimentos, cancelar e manter a assinatura, e cupom.

A Stripe é a de tests/stripe_falso.py, e o e-mail, o `ProvedorEmailFalso` da
fixture `provedor`. As consultas nunca chamam a Stripe; só as ações e a lista de
cupons.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import stripe
from conftest import FRONTEND_URL_TESTE
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from stripe_falso import assinatura_da_stripe, cupom_da_stripe

from app.api.senha import obter_provedor_email_dependencia
from app.core.config import settings
from app.models import (
    AcessoAdmin,
    AssinaturaStripe,
    Conta,
    CupomEnviado,
    EventoConta,
    PagamentoStripe,
    Usuario,
)
from app.services.assinatura import stripe_api
from app.services.email.mensagens import mensagem_cupom
from main import app

client = TestClient(app)

SO_OPERADOR = {"detail": "Acesso restrito à equipe do Ledgr."}
LOGIN_ANTIGO = {"detail": "Entre de novo para usar o painel."}


def _agora_em_segundos() -> int:
    return int(datetime.now(UTC).timestamp())


def _get(headers, rota, **params):
    return client.get(rota, headers=headers, params=params)


def _post(headers, conta_id, caminho, **corpo):
    return client.post(f"/admin/contas/{conta_id}/{caminho}", headers=headers, json=corpo)


def _acessos(db_session) -> list[AcessoAdmin]:
    db_session.expire_all()
    return db_session.scalars(select(AcessoAdmin).order_by(AcessoAdmin.em)).all()


@pytest.fixture(autouse=True)
def _sem_acessos_de_outros_testes(db_session):
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()


def _assinar(
    db_session,
    stripe_falso,
    conta_id,
    *,
    status="active",
    plano="padrao",
    periodo="mensal",
    fim: datetime | None = None,
    agendada=False,
    cancelada_em: datetime | None = None,
    motivo=None,
    origem=None,
    discounts=None,
    sub_id=None,
) -> AssinaturaStripe:
    """A assinatura da conta no banco e, igual, na Stripe falsa."""
    conta = db_session.get(Conta, conta_id)
    if conta.stripe_customer_id is None:
        conta.stripe_customer_id = f"cus_{uuid.uuid4().hex[:14]}"
    sub_id = sub_id or f"sub_{uuid.uuid4().hex[:14]}"
    fim = fim or datetime.now(UTC) + timedelta(days=20)
    stripe_falso.assinaturas[sub_id] = assinatura_da_stripe(
        sub_id,
        conta_id=conta_id,
        cliente_id=conta.stripe_customer_id,
        status=status,
        lookup_key=f"{plano}_{periodo}",
        fim_do_periodo=int(fim.timestamp()),
        cancelar_no_fim=agendada,
        cancelada_em=int(cancelada_em.timestamp()) if cancelada_em else None,
        discounts=discounts,
    )
    assinatura = AssinaturaStripe(
        conta_id=conta_id,
        stripe_subscription_id=sub_id,
        status=status,
        plano=plano,
        periodo=periodo,
        periodo_atual_fim=fim,
        cancelar_no_fim_do_periodo=agendada,
        cancelada_em=cancelada_em,
        motivo_cancelamento=motivo,
        origem_cancelamento=origem,
    )
    db_session.add(assinatura)
    db_session.commit()
    return assinatura


def _pagamento(db_session, conta_id, ocorrido_em, *, status="paga", valor=9900) -> PagamentoStripe:
    pagamento = PagamentoStripe(
        stripe_invoice_id=f"in_{uuid.uuid4().hex}",
        conta_id=conta_id,
        valor_centavos=valor,
        moeda="brl",
        status=status,
        periodo_inicio=ocorrido_em,
        periodo_fim=ocorrido_em + timedelta(days=30),
        ocorrido_em=ocorrido_em,
    )
    db_session.add(pagamento)
    db_session.commit()
    return pagamento


def _eventos(db_session, conta_id) -> list[EventoConta]:
    db_session.expire_all()
    return db_session.scalars(
        select(EventoConta).where(EventoConta.conta_id == conta_id).order_by(EventoConta.em)
    ).all()


def _assinatura_do_banco(db_session, conta_id) -> AssinaturaStripe | None:
    db_session.expire_all()
    return db_session.scalar(select(AssinaturaStripe).where(AssinaturaStripe.conta_id == conta_id))


# --- quem entra -----------------------------------------------------------------------

ROTAS = [
    ("GET", "/admin/contas/{conta_id}/pagamentos", None),
    ("GET", "/admin/pagamentos", None),
    ("GET", "/admin/cancelamentos", None),
    ("GET", "/admin/vencimentos", None),
    ("GET", "/admin/cupons", None),
    ("POST", "/admin/contas/{conta_id}/assinatura/cancelar", {"quando": "agora", "motivo": "x"}),
    ("POST", "/admin/contas/{conta_id}/assinatura/manter", {"motivo": "x"}),
    ("POST", "/admin/contas/{conta_id}/cupom", {"cupom_id": "cupom_20"}),
]


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_quem_nao_e_operador_recebe_403_sem_chamar_a_stripe(
    db_session, cliente, stripe_falso, papel
):
    c = cliente()

    for metodo, rota, corpo in ROTAS:
        resposta = client.request(
            metodo, rota.format(conta_id=c.conta_id), headers=c.headers[papel], json=corpo
        )
        assert (resposta.status_code, resposta.json()) == (403, SO_OPERADOR), rota

    acessos = _acessos(db_session)
    assert len(acessos) == len(ROTAS)
    assert {a.resultado for a in acessos} == {"nao_operador"}
    assert stripe_falso.chamadas == []
    assert db_session.scalars(select(CupomEnviado)).all() == []


def test_login_antigo_e_401_em_todas_as_rotas_e_fica_registrado(db_session, operador, cliente):
    c = cliente()
    antigo = operador(auth_time=_agora_em_segundos() - 2 * 3600 - 60)

    for metodo, rota, corpo in ROTAS:
        resposta = client.request(
            metodo, rota.format(conta_id=c.conta_id), headers=antigo.headers, json=corpo
        )
        assert (resposta.status_code, resposta.json()) == (401, LOGIN_ANTIGO), rota

    acessos = _acessos(db_session)
    assert [a.resultado for a in acessos] == ["login_antigo"] * len(ROTAS)
    assert {a.operador_id for a in acessos} == {antigo.registro.id}


def test_sem_login_e_401(db_session):
    for metodo, rota, corpo in ROTAS:
        resposta = client.request(metodo, rota.format(conta_id=uuid.uuid4()), json=corpo)
        assert resposta.status_code == 401, rota
    assert _acessos(db_session) == []


def test_operador_fica_registrado_com_o_modelo_da_rota_e_a_conta(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    stripe_falso.cupons["cupom_20"] = cupom_da_stripe()

    _get(o.headers, f"/admin/contas/{c.conta_id}/pagamentos")
    _get(o.headers, "/admin/pagamentos", status="falhou")
    _get(o.headers, "/admin/cupons")
    _post(o.headers, c.conta_id, "assinatura/manter", motivo="x")

    assert [
        (a.metodo, a.rota, a.conta_consultada_id, a.resultado) for a in _acessos(db_session)
    ] == [
        ("GET", "/admin/contas/{conta_id}/pagamentos", c.conta_id, "permitido"),
        ("GET", "/admin/pagamentos", None, "permitido"),
        ("GET", "/admin/cupons", None, "permitido"),
        ("POST", "/admin/contas/{conta_id}/assinatura/manter", c.conta_id, "permitido"),
    ]


def test_consultas_nao_chamam_a_stripe(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    stripe_falso.chamadas.clear()

    for rota in (
        f"/admin/contas/{c.conta_id}",
        f"/admin/contas/{c.conta_id}/pagamentos",
        "/admin/pagamentos",
        "/admin/cancelamentos",
        "/admin/vencimentos",
    ):
        assert _get(o.headers, rota).status_code == 200, rota

    assert stripe_falso.chamadas == []


# --- detalhe da conta -----------------------------------------------------------------


def test_detalhe_sem_assinatura_tem_blocos_vazios(operador, cliente):
    o = operador()
    c = cliente()

    corpo = _get(o.headers, f"/admin/contas/{c.conta_id}").json()

    assert corpo["assinatura"] is None
    assert corpo["pagamentos"] == []
    assert corpo["cupons"] == []


def test_detalhe_traz_assinatura_ultimos_12_pagamentos_e_cupons(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    cancelada = datetime(2031, 3, 1, 12, tzinfo=UTC)
    _assinar(
        db_session,
        stripe_falso,
        c.conta_id,
        plano="avancado",
        periodo="anual",
        agendada=True,
        cancelada_em=cancelada,
        motivo="cancellation_requested",
        origem="cliente",
    )
    base = datetime(2031, 1, 1, tzinfo=UTC)
    for i in range(13):
        _pagamento(db_session, c.conta_id, base + timedelta(days=i), valor=1000 + i)
    db_session.add(
        CupomEnviado(
            conta_id=c.conta_id,
            operador_nome="Olga Operadora",
            stripe_coupon_id="cupom_20",
            cupom_nome="Vinte por cento",
            desconto="20% por 3 meses",
            forma="codigo",
            codigo="LEDGR-ABC",
            stripe_promotion_code_id=f"promo_{uuid.uuid4().hex}",
            expira_em=base,
        )
    )
    db_session.commit()

    corpo = _get(o.headers, f"/admin/contas/{c.conta_id}").json()

    assert corpo["assinatura"] == {
        "status": "active",
        "plano": "avancado",
        "periodo": "anual",
        "periodo_atual_fim": corpo["assinatura"]["periodo_atual_fim"],
        "cancelar_no_fim_do_periodo": True,
        "cancelada_em": "2031-03-01T12:00:00Z",
        "motivo_cancelamento": "cancellation_requested",
        "origem_cancelamento": "cliente",
    }
    valores = [p["valor_centavos"] for p in corpo["pagamentos"]]
    assert valores == [1012 - i for i in range(12)]
    assert [c_["codigo"] for c_ in corpo["cupons"]] == ["LEDGR-ABC"]
    assert corpo["cupons"][0]["operador"] == "Olga Operadora"
    assert "stripe_subscription_id" not in str(corpo)
    assert "sub_" not in str(corpo)


# --- pagamentos da conta e globais ----------------------------------------------------


def test_pagamentos_da_conta_paginados_dos_mais_recentes_aos_mais_antigos(
    db_session, operador, cliente
):
    o = operador()
    c = cliente()
    outra = cliente("Outra Conta Ltda")
    base = datetime(2031, 1, 1, tzinfo=UTC)
    for i in range(5):
        _pagamento(db_session, c.conta_id, base + timedelta(days=i), valor=100 + i)
    _pagamento(db_session, outra.conta_id, base, valor=999)

    pagina1 = _get(o.headers, f"/admin/contas/{c.conta_id}/pagamentos", limit=2).json()
    pagina3 = _get(o.headers, f"/admin/contas/{c.conta_id}/pagamentos", limit=2, offset=4).json()
    alem = _get(o.headers, f"/admin/contas/{c.conta_id}/pagamentos", limit=2, offset=10).json()

    assert (pagina1["total"], pagina1["limit"], pagina1["offset"]) == (5, 2, 0)
    assert [p["valor_centavos"] for p in pagina1["itens"]] == [104, 103]
    assert [p["valor_centavos"] for p in pagina3["itens"]] == [100]
    assert (alem["total"], alem["itens"]) == (5, [])
    assert set(pagina1["itens"][0]) == {
        "id",
        "valor_centavos",
        "moeda",
        "status",
        "periodo_inicio",
        "periodo_fim",
        "ocorrido_em",
    }


def test_pagamentos_da_conta_404_e_limites(operador):
    o = operador()

    assert _get(o.headers, f"/admin/contas/{uuid.uuid4()}/pagamentos").status_code == 404
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        resposta = _get(o.headers, f"/admin/contas/{uuid.uuid4()}/pagamentos", **params)
        assert resposta.status_code == 422, params


def test_pagamentos_globais_por_status_e_periodo(db_session, operador, cliente):
    o = operador()
    a = cliente("Alfa Ltda")
    b = cliente("Beta Ltda")
    # 31/10 23:30 em São Paulo já é 01/11 02:30 em UTC: conta como 31/10.
    _pagamento(db_session, a.conta_id, datetime(2031, 11, 1, 2, 30, tzinfo=UTC), valor=1)
    _pagamento(
        db_session, a.conta_id, datetime(2031, 11, 5, 15, tzinfo=UTC), status="falhou", valor=2
    )
    _pagamento(
        db_session, b.conta_id, datetime(2031, 11, 10, 15, tzinfo=UTC), status="falhou", valor=3
    )
    _pagamento(db_session, b.conta_id, datetime(2031, 11, 20, 15, tzinfo=UTC), valor=4)

    def valores(**params):
        corpo = _get(
            o.headers, "/admin/pagamentos", **{"desde": "2031-10-01", "ate": "2031-12-31"} | params
        ).json()
        return [p["valor_centavos"] for p in corpo["itens"]], corpo["total"]

    assert valores() == ([4, 3, 2, 1], 4)
    assert valores(status="falhou") == ([3, 2], 2)
    assert valores(status="paga") == ([4, 1], 2)
    # `ate` inclui o dia inteiro; o dia de São Paulo, e não o de UTC.
    assert valores(desde="2031-10-31", ate="2031-10-31") == ([1], 1)
    assert valores(desde="2031-11-01", ate="2031-11-05") == ([2], 1)
    assert valores(desde="2031-11-10") == ([4, 3], 2)
    assert valores(ate="2031-11-09", desde="2031-11-02") == ([2], 1)
    assert valores(limit=1, offset=1) == ([3], 4)


def test_pagamentos_globais_trazem_a_conta_e_nenhum_dado_de_cliente(db_session, operador, cliente):
    o = operador()
    c = cliente("Escritório Visível Ltda", email="contato.secreto@cliente.com.br")
    _pagamento(db_session, c.conta_id, datetime(2032, 1, 10, tzinfo=UTC), status="falhou")

    resposta = _get(o.headers, "/admin/pagamentos", desde="2032-01-01", ate="2032-01-31")

    item = resposta.json()["itens"][0]
    assert (item["conta_id"], item["razao_social"]) == (str(c.conta_id), "Escritório Visível Ltda")
    assert "contato.secreto" not in resposta.text
    assert "Padaria Segredo" not in resposta.text


def test_pagamentos_globais_validam_filtros(operador):
    o = operador()

    assert _get(o.headers, "/admin/pagamentos", status="paga").status_code == 200
    assert _get(o.headers, "/admin/pagamentos", status="pendente").status_code == 422
    assert _get(o.headers, "/admin/pagamentos", desde="ontem").status_code == 422
    invertido = _get(o.headers, "/admin/pagamentos", desde="2031-02-01", ate="2031-01-01")
    assert invertido.status_code == 422


# --- cancelamentos --------------------------------------------------------------------


def test_cancelamentos_canceladas_e_agendadas_com_filtros(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    contas = {nome: cliente(f"{nome} Ltda") for nome in ("A", "B", "C", "D", "E")}
    hoje = datetime(2031, 5, 1, 12, tzinfo=UTC)
    _assinar(
        db_session,
        stripe_falso,
        contas["A"].conta_id,
        status="canceled",
        cancelada_em=hoje - timedelta(days=10),
        motivo="payment_failed",
        origem="falta_de_pagamento",
        plano="essencial",
    )
    _assinar(
        db_session,
        stripe_falso,
        contas["B"].conta_id,
        agendada=True,
        cancelada_em=hoje - timedelta(days=5),
        motivo="cancellation_requested",
        origem="cliente",
        plano="padrao",
        periodo="trimestral",
    )
    _assinar(
        db_session,
        stripe_falso,
        contas["C"].conta_id,
        status="canceled",
        cancelada_em=hoje,
        motivo="cancellation_requested",
        origem="operador",
        plano="avancado",
    )
    # Andando e sem cancelamento: fora da lista.
    _assinar(db_session, stripe_falso, contas["D"].conta_id)
    # Encerrada por outro caminho, sem cancelamento: fora da lista.
    _assinar(db_session, stripe_falso, contas["E"].conta_id, status="unpaid")

    def lista(**params):
        corpo = _get(
            o.headers,
            "/admin/cancelamentos",
            **{"desde": "2031-04-01", "ate": "2031-06-30"} | params,
        ).json()
        return [
            (i["razao_social"][0], i["situacao"], i["origem_cancelamento"]) for i in corpo["itens"]
        ], corpo["total"]

    assert lista() == (
        [
            ("C", "cancelada", "operador"),
            ("B", "agendada", "cliente"),
            ("A", "cancelada", "falta_de_pagamento"),
        ],
        3,
    )
    assert lista(situacao="agendada")[0] == [("B", "agendada", "cliente")]
    assert [i[0] for i in lista(situacao="cancelada")[0]] == ["C", "A"]
    assert lista(origem="falta_de_pagamento")[0] == [("A", "cancelada", "falta_de_pagamento")]
    assert lista(origem="painel_stripe") == ([], 0)
    assert [i[0] for i in lista(desde="2031-04-25")[0]] == ["C", "B"]
    assert [i[0] for i in lista(ate="2031-04-25")[0]] == ["A"]
    assert lista(limit=1, offset=1) == ([("B", "agendada", "cliente")], 3)


def test_cancelamentos_trazem_motivo_origem_data_e_plano(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    quando = datetime(2032, 2, 3, 15, tzinfo=UTC)
    fim = datetime(2032, 3, 1, tzinfo=UTC)
    _assinar(
        db_session,
        stripe_falso,
        c.conta_id,
        agendada=True,
        cancelada_em=quando,
        fim=fim,
        motivo="cancellation_requested",
        origem="operador",
        plano="padrao",
        periodo="semestral",
    )

    item = _get(o.headers, "/admin/cancelamentos", desde="2032-02-01", ate="2032-02-28").json()[
        "itens"
    ][0]

    assert item == {
        "conta_id": str(c.conta_id),
        "razao_social": "Escritório Cliente Ltda",
        "situacao": "agendada",
        "plano": "padrao",
        "periodo": "semestral",
        "status": "active",
        "data": "2032-02-03T15:00:00Z",
        "periodo_atual_fim": "2032-03-01T00:00:00Z",
        "motivo_cancelamento": "cancellation_requested",
        "origem_cancelamento": "operador",
    }


def test_cancelamentos_validam_filtros(operador):
    o = operador()

    assert _get(o.headers, "/admin/cancelamentos", origem="outra").status_code == 422
    assert _get(o.headers, "/admin/cancelamentos", situacao="talvez").status_code == 422
    assert (
        _get(o.headers, "/admin/cancelamentos", desde="2031-02-01", ate="2031-01-01").status_code
        == 422
    )


# --- vencimentos ----------------------------------------------------------------------


def _meus(corpo, *contas):
    ids = {str(c.conta_id) for c in contas}
    return [
        (i["tipo"], i["razao_social"][0], i["cancelamento_agendado"])
        for i in corpo["itens"]
        if i["conta_id"] in ids
    ]


def test_vencimentos_assinaturas_e_testes_na_janela_do_mais_proximo_ao_mais_longe(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    agora = datetime.now(UTC)
    c = {n: cliente(f"{n} Ltda") for n in "ABCDEFGHI"}
    _assinar(db_session, stripe_falso, c["A"].conta_id, fim=agora + timedelta(days=5))
    _assinar(
        db_session, stripe_falso, c["B"].conta_id, status="trialing", fim=agora + timedelta(days=2)
    )
    _assinar(
        db_session, stripe_falso, c["C"].conta_id, fim=agora + timedelta(days=3), agendada=True
    )
    # Fora: depois da janela, já vencida, past_due, canceled.
    _assinar(db_session, stripe_falso, c["D"].conta_id, fim=agora + timedelta(days=8))
    _assinar(db_session, stripe_falso, c["E"].conta_id, fim=agora - timedelta(days=1))
    _assinar(
        db_session, stripe_falso, c["F"].conta_id, status="past_due", fim=agora + timedelta(days=1)
    )
    _assinar(
        db_session, stripe_falso, c["G"].conta_id, status="canceled", fim=agora + timedelta(days=1)
    )
    # Teste: dentro e fora da janela.
    db_session.execute(
        text("UPDATE contas SET teste_termina_em = :t WHERE id = :i"),
        {"t": agora + timedelta(days=4), "i": c["H"].conta_id},
    )
    db_session.execute(
        text("UPDATE contas SET teste_termina_em = :t WHERE id = :i"),
        {"t": agora + timedelta(days=9), "i": c["I"].conta_id},
    )
    db_session.commit()

    corpo = _get(o.headers, "/admin/vencimentos", dias=7).json()

    assert corpo["dias"] == 7
    assert _meus(corpo, *c.values()) == [
        ("assinatura", "B", False),
        ("assinatura", "C", True),
        ("teste", "H", False),
        ("assinatura", "A", False),
    ]
    datas = [i["vence_em"] for i in corpo["itens"]]
    assert datas == sorted(datas)
    assert corpo["total"] >= 4
    amplo = _get(o.headers, "/admin/vencimentos", dias=60).json()
    assert {n for _, n, _ in _meus(amplo, *c.values())} == {"A", "B", "C", "D", "H", "I"}


def test_vencimentos_teste_de_quem_ja_assina_nao_entra_como_teste_e_campos(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    agora = datetime.now(UTC)
    assina = cliente("Assina Ltda")
    sem_assinatura = cliente("Teste Ltda")
    _assinar(
        db_session,
        stripe_falso,
        assina.conta_id,
        fim=agora + timedelta(days=6),
        plano="avancado",
        periodo="anual",
    )
    for conta_id in (assina.conta_id, sem_assinatura.conta_id):
        db_session.execute(
            text("UPDATE contas SET teste_termina_em = :t WHERE id = :i"),
            {"t": agora + timedelta(days=1), "i": conta_id},
        )
    db_session.commit()

    itens = {
        i["conta_id"]: i for i in _get(o.headers, "/admin/vencimentos", dias=7).json()["itens"]
    }

    assert itens[str(assina.conta_id)]["tipo"] == "assinatura"
    assert (itens[str(assina.conta_id)]["plano"], itens[str(assina.conta_id)]["periodo"]) == (
        "avancado",
        "anual",
    )
    teste = itens[str(sem_assinatura.conta_id)]
    assert (teste["tipo"], teste["status"], teste["periodo"], teste["inativa"]) == (
        "teste",
        None,
        None,
        False,
    )
    assert [i["tipo"] for i in itens.values() if i["conta_id"] == str(assina.conta_id)] == [
        "assinatura"
    ]


def test_vencimentos_paginados_e_com_limites_de_dias(db_session, operador, cliente, stripe_falso):
    o = operador()
    agora = datetime.now(UTC)
    for dia in (1, 2, 3):
        c = cliente(f"V{dia} Ltda")
        _assinar(db_session, stripe_falso, c.conta_id, fim=agora + timedelta(days=dia, hours=-1))

    cheio = _get(o.headers, "/admin/vencimentos", dias=7).json()
    pagina = _get(o.headers, "/admin/vencimentos", dias=7, limit=1, offset=1).json()

    assert pagina["itens"] == cheio["itens"][1:2]
    assert pagina["total"] == cheio["total"]
    assert _get(o.headers, "/admin/vencimentos").json()["dias"] == 7
    for dias, esperado in ((0, 422), (61, 422), (1, 200), (60, 200)):
        assert _get(o.headers, "/admin/vencimentos", dias=dias).status_code == esperado, dias


def test_vencimentos_conta_inativa_aparece_marcada(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id, fim=datetime.now(UTC) + timedelta(days=2))
    db_session.execute(
        text("UPDATE contas SET inativada_em = now(), motivo_inativacao = 'x' WHERE id = :i"),
        {"i": c.conta_id},
    )
    db_session.commit()

    itens = _get(o.headers, "/admin/vencimentos").json()["itens"]

    assert [i["inativa"] for i in itens if i["conta_id"] == str(c.conta_id)] == [True]


def test_consultas_nao_logam_nome_email_cnpj_nem_busca(
    db_session, operador, cliente, stripe_falso, caplog
):
    o = operador()
    c = cliente("Razão Secreta Ltda", email="contato.secreto@cliente.com.br")
    _assinar(db_session, stripe_falso, c.conta_id, fim=datetime.now(UTC) + timedelta(days=2))
    _pagamento(db_session, c.conta_id, datetime.now(UTC), status="falhou")
    cnpj = db_session.get(Conta, c.conta_id).cnpj

    with caplog.at_level(logging.DEBUG):
        for rota in (
            f"/admin/contas/{c.conta_id}",
            f"/admin/contas/{c.conta_id}/pagamentos",
            "/admin/pagamentos",
            "/admin/cancelamentos",
            "/admin/vencimentos",
        ):
            _get(o.headers, rota, desde="2000-01-01")

    # Só os logs do app: o cliente HTTP do teste loga a URL da própria requisição.
    do_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "admin_acesso" in do_app
    for segredo in ("Razão Secreta", "contato.secreto", cnpj, "2000-01-01"):
        assert segredo not in do_app


# --- cancelar -------------------------------------------------------------------------


def test_cancelar_no_fim_do_periodo(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    antes = _assinar(db_session, stripe_falso, c.conta_id)
    sub_id = antes.stripe_subscription_id

    resposta = _post(
        o.headers,
        c.conta_id,
        "assinatura/cancelar",
        quando="fim_do_periodo",
        motivo="  Pediu por telefone  ",
    )

    assert resposta.status_code == 200
    # Só `modify`, com a marca do operador, sem tocar em proporcional nem reembolso.
    assert stripe_falso.chamadas_de("Subscription.cancel") == []
    (alteracao,) = stripe_falso.chamadas_de("Subscription.modify")
    assert alteracao["id"] == sub_id
    assert alteracao["cancel_at_period_end"] is True
    assert alteracao["cancellation_details"] == {"comment": stripe_api.MARCA_DO_OPERADOR}
    assert (alteracao["api_key"], alteracao["stripe_version"]) == (
        "sk_test_falsa",
        stripe_api.API_VERSION,
    )
    assert "Pediu por telefone" not in str(alteracao)
    # Sincronizado na hora, pela mesma função do webhook: busca a assinatura de novo.
    assert stripe_falso.nomes()[-2:] == ["Subscription.modify", "Subscription.retrieve"]
    banco = _assinatura_do_banco(db_session, c.conta_id)
    assert banco.status == "active"
    assert banco.cancelar_no_fim_do_periodo is True
    assert banco.cancelada_em is not None
    assert (banco.motivo_cancelamento, banco.origem_cancelamento) == (
        "cancellation_requested",
        "operador",
    )
    corpo = resposta.json()
    assert corpo["assinatura"]["cancelar_no_fim_do_periodo"] is True
    assert corpo["assinatura"]["origem_cancelamento"] == "operador"
    assert corpo["situacao"] == "ativo"
    (evento,) = _eventos(db_session, c.conta_id)
    assert (evento.tipo, evento.motivo, evento.origem) == (
        "cancelamento_agendado",
        "Pediu por telefone",
        "painel",
    )
    assert (evento.operador_id, evento.operador_nome) == (o.registro.id, o.usuario.nome)
    assert corpo["eventos"][0]["tipo"] == "cancelamento_agendado"


def test_cancelar_agora_nao_gera_fatura_nem_proporcional(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    antes = _assinar(db_session, stripe_falso, c.conta_id)

    resposta = _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="Fraude")

    assert resposta.status_code == 200
    assert stripe_falso.chamadas_de("Subscription.modify") == []
    (cancelamento,) = stripe_falso.chamadas_de("Subscription.cancel")
    assert cancelamento["id"] == antes.stripe_subscription_id
    assert cancelamento["invoice_now"] is False
    assert cancelamento["prorate"] is False
    assert cancelamento["cancellation_details"] == {"comment": stripe_api.MARCA_DO_OPERADOR}
    assert stripe_falso.nomes()[-2:] == ["Subscription.cancel", "Subscription.retrieve"]
    banco = _assinatura_do_banco(db_session, c.conta_id)
    assert banco.status == "canceled"
    assert banco.cancelada_em is not None
    assert (banco.motivo_cancelamento, banco.origem_cancelamento) == (
        "cancellation_requested",
        "operador",
    )
    assert resposta.json()["situacao"] == "assinatura_encerrada"
    (evento,) = _eventos(db_session, c.conta_id)
    assert (evento.tipo, evento.motivo) == ("assinatura_cancelada", "Fraude")


def test_cancelar_agora_vale_mesmo_com_o_cancelamento_agendado(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    _post(o.headers, c.conta_id, "assinatura/cancelar", quando="fim_do_periodo", motivo="a")

    resposta = _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="b")

    assert resposta.status_code == 200
    assert [e.tipo for e in _eventos(db_session, c.conta_id)] == [
        "cancelamento_agendado",
        "assinatura_cancelada",
    ]
    assert _assinatura_do_banco(db_session, c.conta_id).status == "canceled"


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_cancelar_funciona_nas_tres_situacoes_vivas(
    db_session, operador, cliente, stripe_falso, status
):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id, status=status)

    assert (
        _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="x").status_code
        == 200
    )


def test_cancelar_sem_assinatura_ou_ja_cancelada_e_409_sem_chamar_a_stripe(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    sem = cliente("Sem Ltda")
    cancelada = cliente("Cancelada Ltda")
    _assinar(db_session, stripe_falso, cancelada.conta_id, status="canceled")
    stripe_falso.chamadas.clear()

    for conta in (sem, cancelada):
        for quando in ("fim_do_periodo", "agora"):
            resposta = _post(
                o.headers, conta.conta_id, "assinatura/cancelar", quando=quando, motivo="x"
            )
            assert (resposta.status_code, resposta.json()) == (
                409,
                {"detail": "A conta não tem uma assinatura ativa."},
            )

    assert stripe_falso.chamadas == []
    assert _eventos(db_session, sem.conta_id) == []


def test_cancelar_no_fim_do_periodo_duas_vezes_e_409(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    _post(o.headers, c.conta_id, "assinatura/cancelar", quando="fim_do_periodo", motivo="a")
    chamadas = len(stripe_falso.chamadas)

    resposta = _post(
        o.headers, c.conta_id, "assinatura/cancelar", quando="fim_do_periodo", motivo="b"
    )

    assert resposta.status_code == 409
    assert len(stripe_falso.chamadas) == chamadas
    assert len(_eventos(db_session, c.conta_id)) == 1


@pytest.mark.parametrize(
    "corpo",
    [
        {"quando": "agora"},
        {"quando": "agora", "motivo": "   "},
        {"quando": "agora", "motivo": "x" * 501},
        {"quando": "depois", "motivo": "x"},
        {"motivo": "x"},
        {"quando": "agora", "motivo": "x", "reembolso": True},
    ],
)
def test_cancelar_exige_quando_e_motivo(db_session, operador, cliente, stripe_falso, corpo):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    stripe_falso.chamadas.clear()

    assert _post(o.headers, c.conta_id, "assinatura/cancelar", **corpo).status_code == 422
    assert stripe_falso.chamadas == []


def test_cancelar_conta_que_nao_existe_e_404(operador, stripe_falso):
    resposta = _post(
        operador().headers, uuid.uuid4(), "assinatura/cancelar", quando="agora", motivo="x"
    )

    assert resposta.status_code == 404


def test_cancelar_com_a_stripe_fora_do_ar_e_502_e_nao_grava(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    stripe_falso.erro = stripe.APIConnectionError("sem rede para api.stripe.com")

    resposta = _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="x")

    assert resposta.status_code == 502
    assert "api.stripe.com" not in resposta.text
    assert _eventos(db_session, c.conta_id) == []
    assert _assinatura_do_banco(db_session, c.conta_id).status == "active"


def test_acoes_com_a_cobranca_desligada_dao_503(db_session, operador, cliente, monkeypatch):
    o = operador()
    c = cliente()
    monkeypatch.setattr(settings, "stripe_habilitado", False)

    for caminho, corpo in (
        ("assinatura/cancelar", {"quando": "agora", "motivo": "x"}),
        ("assinatura/manter", {"motivo": "x"}),
        ("cupom", {"cupom_id": "cupom_20"}),
    ):
        assert _post(o.headers, c.conta_id, caminho, **corpo).status_code == 503, caminho
    assert _get(o.headers, "/admin/cupons").status_code == 503


def test_o_webhook_que_chega_depois_nao_muda_o_que_o_painel_gravou(
    db_session, operador, cliente, stripe_falso
):
    from test_stripe_webhook import _enviar, _evento

    o = operador()
    c = cliente()
    antes = _assinar(db_session, stripe_falso, c.conta_id)
    _post(o.headers, c.conta_id, "assinatura/cancelar", quando="fim_do_periodo", motivo="x")
    estado = _assinatura_do_banco(db_session, c.conta_id)
    esperado = (
        estado.status,
        estado.cancelar_no_fim_do_periodo,
        estado.motivo_cancelamento,
        estado.origem_cancelamento,
    )

    resposta = _enviar(
        _evento("customer.subscription.updated", {"id": antes.stripe_subscription_id})
    )

    assert resposta.status_code == 200
    banco = _assinatura_do_banco(db_session, c.conta_id)
    assert (
        banco.status,
        banco.cancelar_no_fim_do_periodo,
        banco.motivo_cancelamento,
        banco.origem_cancelamento,
    ) == esperado


# --- manter ---------------------------------------------------------------------------


def test_manter_desfaz_o_cancelamento_agendado(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    antes = _assinar(db_session, stripe_falso, c.conta_id)
    _post(o.headers, c.conta_id, "assinatura/cancelar", quando="fim_do_periodo", motivo="a")

    resposta = _post(o.headers, c.conta_id, "assinatura/manter", motivo="  Cliente voltou atrás ")

    assert resposta.status_code == 200
    desfazer = stripe_falso.chamadas_de("Subscription.modify")[-1]
    assert desfazer["id"] == antes.stripe_subscription_id
    assert desfazer["cancel_at_period_end"] is False
    # Limpa a marca, para um cancelamento futuro não herdar a origem "operador".
    assert desfazer["cancellation_details"] == {"comment": ""}
    assert stripe_falso.nomes()[-2:] == ["Subscription.modify", "Subscription.retrieve"]
    banco = _assinatura_do_banco(db_session, c.conta_id)
    assert banco.cancelar_no_fim_do_periodo is False
    assert (banco.motivo_cancelamento, banco.origem_cancelamento, banco.cancelada_em) == (
        None,
        None,
        None,
    )
    assert resposta.json()["assinatura"]["cancelar_no_fim_do_periodo"] is False
    eventos = _eventos(db_session, c.conta_id)
    assert [(e.tipo, e.motivo) for e in eventos] == [
        ("cancelamento_agendado", "a"),
        ("cancelamento_desfeito", "Cliente voltou atrás"),
    ]
    assert eventos[1].operador_id == o.registro.id


def test_manter_sem_cancelamento_agendado_ou_sem_assinatura_e_409(
    db_session, operador, cliente, stripe_falso
):
    o = operador()
    andando = cliente("Andando Ltda")
    sem = cliente("Sem Ltda")
    cancelada = cliente("Cancelada Ltda")
    _assinar(db_session, stripe_falso, andando.conta_id)
    _assinar(db_session, stripe_falso, cancelada.conta_id, status="canceled", agendada=True)
    stripe_falso.chamadas.clear()

    respostas = {
        nome: _post(o.headers, conta.conta_id, "assinatura/manter", motivo="x")
        for nome, conta in (("andando", andando), ("sem", sem), ("cancelada", cancelada))
    }

    assert {n: r.status_code for n, r in respostas.items()} == {
        "andando": 409,
        "sem": 409,
        "cancelada": 409,
    }
    assert respostas["andando"].json() == {"detail": "A assinatura não tem cancelamento agendado."}
    assert respostas["sem"].json() == {"detail": "A conta não tem uma assinatura ativa."}
    assert stripe_falso.chamadas == []


def test_manter_exige_motivo(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id, agendada=True)

    assert _post(o.headers, c.conta_id, "assinatura/manter").status_code == 422
    assert _post(o.headers, c.conta_id, "assinatura/manter", motivo=" ").status_code == 422
    assert stripe_falso.chamadas == []


def test_manter_com_a_stripe_fora_do_ar_e_502(db_session, operador, cliente, stripe_falso):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id, agendada=True)
    stripe_falso.erro = stripe.APIConnectionError("fora")

    assert _post(o.headers, c.conta_id, "assinatura/manter", motivo="x").status_code == 502
    assert _eventos(db_session, c.conta_id) == []
    assert _assinatura_do_banco(db_session, c.conta_id).cancelar_no_fim_do_periodo is True


# --- cupons da Stripe -----------------------------------------------------------------


def test_lista_de_cupons_so_os_validos_com_cache_curto(operador, stripe_falso):
    o = operador()
    stripe_falso.cupons["cupom_20"] = cupom_da_stripe("cupom_20")
    stripe_falso.cupons["cupom_50"] = cupom_da_stripe(
        "cupom_50", nome="Cinquenta reais", percent_off=None, amount_off=5000, duration="once"
    )
    stripe_falso.cupons["cupom_sempre"] = cupom_da_stripe(
        "cupom_sempre", nome=None, percent_off=12.5, duration="forever"
    )
    stripe_falso.cupons["cupom_velho"] = cupom_da_stripe("cupom_velho", valido=False)

    corpo = _get(o.headers, "/admin/cupons").json()

    assert corpo == [
        {
            "id": "cupom_20",
            "nome": "Vinte por cento",
            "percent_off": 20.0,
            "amount_off": None,
            "moeda": None,
            "duration": "repeating",
            "duration_in_months": 3,
            "resumo": "20% por 3 meses",
        },
        {
            "id": "cupom_50",
            "nome": "Cinquenta reais",
            "percent_off": None,
            "amount_off": 5000,
            "moeda": "brl",
            "duration": "once",
            "duration_in_months": None,
            "resumo": "R$ 50,00 em uma cobrança",
        },
        {
            "id": "cupom_sempre",
            "nome": "cupom_sempre",
            "percent_off": 12.5,
            "amount_off": None,
            "moeda": None,
            "duration": "forever",
            "duration_in_months": None,
            "resumo": "12,5% para sempre",
        },
    ]
    _get(o.headers, "/admin/cupons")
    assert len(stripe_falso.chamadas_de("Coupon.list")) == 1


def test_lista_de_cupons_serve_o_cache_se_a_stripe_cair_e_da_502_sem_nada_guardado(
    operador, stripe_falso, monkeypatch
):
    from app.services.assinatura import cupons

    o = operador()
    stripe_falso.erro = stripe.APIConnectionError("fora")
    assert _get(o.headers, "/admin/cupons").status_code == 502

    stripe_falso.erro = None
    stripe_falso.cupons["cupom_20"] = cupom_da_stripe()
    assert len(_get(o.headers, "/admin/cupons").json()) == 1
    monkeypatch.setattr(cupons, "VALIDADE_DO_CACHE", timedelta(0))
    stripe_falso.erro = stripe.APIConnectionError("fora")

    assert len(_get(o.headers, "/admin/cupons").json()) == 1


# --- dar cupom ------------------------------------------------------------------------


@pytest.fixture
def cupom_na_stripe(stripe_falso):
    stripe_falso.cupons["cupom_20"] = cupom_da_stripe()
    return stripe_falso.cupons["cupom_20"]


def _cupons(db_session, conta_id) -> list[CupomEnviado]:
    db_session.expire_all()
    return db_session.scalars(select(CupomEnviado).where(CupomEnviado.conta_id == conta_id)).all()


def test_cupom_sem_assinatura_cria_um_codigo_so_da_conta_e_manda_por_email(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, provedor
):
    o = operador()
    c = cliente("Escritório Cliente Ltda", email="admin@cliente.com.br")
    antes = datetime.now(UTC)

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=45)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["forma"] == "codigo"
    assert corpo["codigo"].startswith("LEDGR-") and len(corpo["codigo"]) == 14
    assert (corpo["cupom"], corpo["desconto"], corpo["email_enviado"]) == (
        "Vinte por cento",
        "20% por 3 meses",
        True,
    )
    expira = datetime.fromisoformat(corpo["expira_em"])
    assert (
        timedelta(days=45) - timedelta(minutes=1) <= expira - antes <= timedelta(days=45, minutes=1)
    )

    # O Customer da conta nasceu aqui, pela mesma função do checkout, e é dele o código.
    conta = db_session.get(Conta, c.conta_id)
    (criacao,) = stripe_falso.chamadas_de("Customer.create")
    assert criacao["idempotency_key"] == f"ledgr-cliente-{c.conta_id}"
    assert criacao["email"] == "admin@cliente.com.br"
    (codigo,) = stripe_falso.chamadas_de("PromotionCode.create")
    assert codigo["promotion"] == {"type": "coupon", "coupon": "cupom_20"}
    assert codigo["customer"] == conta.stripe_customer_id
    assert codigo["max_redemptions"] == 1
    assert codigo["expires_at"] == int(expira.timestamp())
    assert codigo["metadata"] == {"conta_id": str(c.conta_id), "operador_id": str(o.registro.id)}
    assert codigo["code"] == corpo["codigo"]
    assert codigo["idempotency_key"].startswith(f"ledgr-cupom-codigo-{c.conta_id}-cupom_20-")
    assert (codigo["api_key"], codigo["stripe_version"]) == (
        "sk_test_falsa",
        stripe_api.API_VERSION,
    )
    assert stripe_falso.chamadas_de("Subscription.modify") == []

    (registro,) = _cupons(db_session, c.conta_id)
    assert (registro.forma, registro.codigo, registro.email_enviado, registro.usado_em) == (
        "codigo",
        corpo["codigo"],
        True,
        None,
    )
    assert registro.stripe_promotion_code_id.startswith("promo_")
    assert (registro.stripe_coupon_id, registro.cupom_nome, registro.desconto) == (
        "cupom_20",
        "Vinte por cento",
        "20% por 3 meses",
    )
    assert (registro.operador_id, registro.operador_nome) == (o.registro.id, o.usuario.nome)
    assert registro.expira_em == expira

    (evento,) = _eventos(db_session, c.conta_id)
    assert evento.tipo == "cupom_enviado"
    assert "Vinte por cento" in evento.motivo and corpo["codigo"] not in evento.motivo

    (email,) = provedor.enviadas
    assert email.para == "admin@cliente.com.br"
    assert corpo["codigo"] in email.texto and corpo["codigo"] in email.html
    assert "20% por 3 meses" in email.texto
    assert (
        expira.astimezone(__import__("zoneinfo").ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")
        in email.texto
    )
    assert f"{FRONTEND_URL_TESTE}/assinatura" in email.texto
    assert "Escritório Cliente Ltda" in email.texto


def test_cupom_validade_padrao_de_30_dias_e_limites(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    o = operador()
    c = cliente()

    padrao = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20").json()

    dias = (datetime.fromisoformat(padrao["expira_em"]) - datetime.now(UTC)).total_seconds() / 86400
    assert 29.99 < dias <= 30
    for invalida in (0, 91, -1):
        resposta = _post(
            o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=invalida
        )
        assert resposta.status_code == 422, invalida
    assert (
        _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=1).status_code
        == 200
    )
    assert (
        _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=90).status_code
        == 200
    )
    assert _post(o.headers, c.conta_id, "cupom").status_code == 422
    assert (
        _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", forma="assinatura").status_code
        == 422
    )


def test_validade_do_codigo_nunca_passa_do_limite_do_proprio_cupom(
    db_session, operador, cliente, stripe_falso
):
    limite = int((datetime.now(UTC) + timedelta(days=5)).timestamp())
    stripe_falso.cupons["cupom_20"] = cupom_da_stripe(redeem_by=limite)
    o = operador()
    c = cliente()

    corpo = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=60).json()

    assert int(datetime.fromisoformat(corpo["expira_em"]).timestamp()) == limite


def test_cupom_reusa_o_customer_que_a_conta_ja_tem(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    o = operador()
    c = cliente()
    db_session.get(Conta, c.conta_id).stripe_customer_id = "cus_existente"
    db_session.commit()

    _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")

    assert stripe_falso.chamadas_de("Customer.create") == []
    assert stripe_falso.chamadas_de("PromotionCode.create")[0]["customer"] == "cus_existente"


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_cupom_com_assinatura_viva_vai_direto_na_assinatura_sem_codigo(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, provedor, status
):
    o = operador()
    c = cliente("Escritório Cliente Ltda", email="admin@cliente.com.br")
    assinatura = _assinar(
        db_session, stripe_falso, c.conta_id, status=status, discounts=["di_antigo"]
    )

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20", validade_dias=10)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert (corpo["forma"], corpo["codigo"], corpo["expira_em"], corpo["email_enviado"]) == (
        "assinatura",
        None,
        None,
        True,
    )
    # Sem código nem Customer novo: o desconto entra na assinatura, e o que ela já tinha fica.
    assert stripe_falso.chamadas_de("PromotionCode.create") == []
    assert stripe_falso.chamadas_de("Customer.create") == []
    (aplicacao,) = stripe_falso.chamadas_de("Subscription.modify")
    assert aplicacao["id"] == assinatura.stripe_subscription_id
    assert aplicacao["discounts"] == [{"discount": "di_antigo"}, {"coupon": "cupom_20"}]
    assert aplicacao["idempotency_key"].startswith(f"ledgr-cupom-assinatura-{c.conta_id}-cupom_20-")
    assert len(stripe_falso.assinaturas[assinatura.stripe_subscription_id]["discounts"]) == 2
    (registro,) = _cupons(db_session, c.conta_id)
    assert (
        registro.forma,
        registro.codigo,
        registro.stripe_promotion_code_id,
        registro.expira_em,
    ) == ("assinatura", None, None, None)
    assert registro.usado_em is not None
    (email,) = provedor.enviadas
    assert "aplicou um desconto na assinatura" in email.texto and "20% por 3 meses" in email.texto
    assert "LEDGR-" not in email.texto and "código" not in email.texto.lower().split(
        "equipe ledgr"
    )[0].replace("tela assinatura", "")


@pytest.mark.parametrize("status", ["canceled", "unpaid", "incomplete_expired"])
def test_cupom_com_assinatura_encerrada_vira_codigo(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, status
):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id, status=status)

    assert _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20").json()["forma"] == "codigo"


def test_cupom_com_o_email_desligado_funciona_e_so_nao_envia(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    # Sem a fixture `provedor`: `EMAIL_HABILITADO` desligado, como na suíte toda.
    o = operador()
    c = cliente()

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["codigo"].startswith("LEDGR-") and corpo["email_enviado"] is False
    (registro,) = _cupons(db_session, c.conta_id)
    assert (registro.codigo, registro.email_enviado) == (corpo["codigo"], False)


def test_cupom_com_o_email_falhando_nao_desfaz_o_cupom(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, provedor
):
    provedor._falha = RuntimeError("Resend fora do ar para contato@cliente.com.br")
    o = operador()
    c = cliente(email="contato@cliente.com.br")

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["email_enviado"] is False and corpo["codigo"].startswith("LEDGR-")
    (registro,) = _cupons(db_session, c.conta_id)
    assert (registro.codigo, registro.email_enviado) == (corpo["codigo"], False)
    assert [e.tipo for e in _eventos(db_session, c.conta_id)] == ["cupom_enviado"]


def test_cupom_com_o_email_ligado_so_se_o_provedor_existir(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: None
    try:
        o = operador()
        c = cliente()
        resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")
    finally:
        app.dependency_overrides.pop(obter_provedor_email_dependencia, None)

    assert resposta.json()["email_enviado"] is False


def test_cupom_inexistente_ou_invalido_e_404_e_nao_cria_nada(
    db_session, operador, cliente, stripe_falso, provedor
):
    stripe_falso.cupons["cupom_velho"] = cupom_da_stripe("cupom_velho", valido=False)
    o = operador()
    c = cliente()

    for cupom_id in ("nao_existe", "cupom_velho"):
        resposta = _post(o.headers, c.conta_id, "cupom", cupom_id=cupom_id)
        assert resposta.status_code == 404, cupom_id
        assert resposta.json() == {"detail": "Cupom não encontrado ou não está mais válido."}

    assert stripe_falso.chamadas_de("PromotionCode.create") == []
    assert stripe_falso.chamadas_de("Customer.create") == []
    assert _cupons(db_session, c.conta_id) == [] and _eventos(db_session, c.conta_id) == []
    assert provedor.enviadas == []


def test_cupom_com_a_stripe_fora_do_ar_e_502_e_nao_grava(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, provedor
):
    o = operador()
    c = cliente()
    stripe_falso.erro = stripe.APIConnectionError("sem rede para api.stripe.com")

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")

    assert resposta.status_code == 502
    assert "api.stripe.com" not in resposta.text
    assert _cupons(db_session, c.conta_id) == [] and _eventos(db_session, c.conta_id) == []
    assert provedor.enviadas == []


def test_cupom_com_a_stripe_recusando_na_criacao_do_codigo_e_502(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, monkeypatch
):
    original = stripe_falso.promotion_code_create

    def recusa(**params):
        original(**params)
        raise stripe.InvalidRequestError("The coupon cannot be applied", param="promotion")

    monkeypatch.setattr(stripe.PromotionCode, "create", staticmethod(recusa))
    o = operador()
    c = cliente()

    assert _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20").status_code == 502
    assert _cupons(db_session, c.conta_id) == []


def test_cupom_de_conta_sem_admin_ativo_e_409(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    o = operador()
    c = cliente()
    admin = db_session.get(Usuario, c.usuarios["admin"].id)
    admin.removido_em, admin.senha_hash = datetime.now(UTC), None
    db_session.commit()

    resposta = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")

    assert resposta.status_code == 409
    assert stripe_falso.chamadas == []


def test_cupom_conta_que_nao_existe_e_404(operador, stripe_falso, cupom_na_stripe):
    assert _post(operador().headers, uuid.uuid4(), "cupom", cupom_id="cupom_20").status_code == 404


def test_cupom_nao_loga_codigo_email_nem_razao_social(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe, provedor, caplog
):
    o = operador()
    c = cliente("Razão Secreta Ltda", email="contato.secreto@cliente.com.br")

    with caplog.at_level(logging.DEBUG):
        corpo = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20").json()
        db_session.expire_all()
        _assinar(db_session, stripe_falso, c.conta_id)
        cancelamento = _post(
            o.headers,
            c.conta_id,
            "assinatura/cancelar",
            quando="agora",
            motivo="Texto livre do operador",
        )
        assert cancelamento.status_code == 200, cancelamento.text

    do_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "admin_cupom" in do_app and "admin_assinatura" in do_app
    for segredo in (corpo["codigo"], "contato.secreto", "Razão Secreta", "Texto livre do operador"):
        assert segredo not in do_app


def test_cupom_aparece_no_detalhe_da_conta(
    db_session, operador, cliente, stripe_falso, cupom_na_stripe
):
    o = operador()
    c = cliente()
    dado = _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20").json()

    cupons = _get(o.headers, f"/admin/contas/{c.conta_id}").json()["cupons"]

    assert [
        (i["id"], i["forma"], i["codigo"], i["email_enviado"], i["usado_em"]) for i in cupons
    ] == [(dado["id"], "codigo", dado["codigo"], False, None)]


# --- e-mail ---------------------------------------------------------------------------


def test_mensagem_do_cupom_escapa_o_html_e_funciona_sem_link():
    com_codigo = mensagem_cupom(
        para="a@b.com",
        nome="Ana <b>",
        razao_social="A & B Ltda",
        desconto="20%",
        codigo="LEDGR-ABC",
        validade="31/12/2031",
        link_assinatura=None,
    )
    sem_codigo = mensagem_cupom(
        para="a@b.com",
        nome="Ana",
        razao_social="A",
        desconto="20%",
        codigo=None,
        validade=None,
        link_assinatura="https://app.ledgr.com.br/assinatura",
    )

    assert "Ana &lt;b&gt;" in com_codigo.html and "A &amp; B Ltda" in com_codigo.html
    assert "31/12/2031" in com_codigo.texto and "LEDGR-ABC" in com_codigo.texto
    assert "https://" not in com_codigo.texto
    assert (
        "LEDGR" not in sem_codigo.texto and "https://app.ledgr.com.br/assinatura" in sem_codigo.html
    )
