"""Cidade e UF no painel administrativo (issue #184, sobre as issues #156 e
#169): na lista e no detalhe das contas, o filtro por UF e a contagem por UF
(`GET /admin/contas/por-uf`).

"Assinando" é a situação `ativo` do painel (piloto ligado ou assinatura da
Stripe `active`/`trialing`), de conta não inativa: a mesma do filtro
`situacao=ativo`. O banco de teste pode ter contas de outros testes, então a
contagem é conferida pela diferença antes e depois de criar as contas do teste.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text

from app.core.localizacao import UFS
from app.models import AssinaturaStripe, Conta
from main import app

client = TestClient(app)

SO_OPERADOR = {"detail": "Acesso restrito à equipe do Ledgr."}
ROTAS_DE_LEITURA = ["/admin/contas", "/admin/contas/por-uf"]


@pytest.fixture(autouse=True)
def _sem_acessos_de_outros_testes(db_session):
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()


@pytest.fixture
def nova_conta(db_session, criar_conta):
    """Conta real com `cidade`/`uf` e a situação pedida: `piloto`, `inativa`
    ou `stripe` (o status da assinatura da Stripe)."""

    def _nova(
        uf=None, cidade=None, razao_social="Conta de UF", piloto=False, inativa=False, stripe=None
    ) -> Conta:
        conta = criar_conta(razao_social=razao_social)
        conta.uf, conta.cidade, conta.assinatura_ativa = uf, cidade, piloto
        if inativa:
            conta.inativada_em, conta.motivo_inativacao = func.now(), "teste"
        if stripe:
            db_session.add(
                AssinaturaStripe(
                    conta_id=conta.id,
                    stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                    status=stripe,
                    plano="padrao",
                    periodo="mensal",
                )
            )
        db_session.commit()
        return conta

    return _nova


def _contas(headers, **params):
    return client.get("/admin/contas", headers=headers, params=params)


def _por_uf(headers):
    return client.get("/admin/contas/por-uf", headers=headers)


def _grupo(corpo: dict, uf: str) -> dict:
    return next((g for g in corpo["ufs"] if g["uf"] == uf), {"contas": 0, "assinando": 0})


# --- lista e detalhe -----------------------------------------------------------------------


def test_lista_e_detalhe_trazem_cidade_e_uf(operador, nova_conta):
    o = operador()
    com = nova_conta(uf="SP", cidade="Ribeirão Preto", razao_social="Com Local Ltda")
    sem = nova_conta(razao_social="Sem Local Ltda")

    itens = {i["id"]: i for i in _contas(o.headers, busca="Local Ltda", limit=100).json()["itens"]}
    detalhe_com = client.get(f"/admin/contas/{com.id}", headers=o.headers).json()
    detalhe_sem = client.get(f"/admin/contas/{sem.id}", headers=o.headers).json()

    assert (itens[str(com.id)]["cidade"], itens[str(com.id)]["uf"]) == ("Ribeirão Preto", "SP")
    assert (itens[str(sem.id)]["cidade"], itens[str(sem.id)]["uf"]) == (None, None)
    assert (detalhe_com["cidade"], detalhe_com["uf"]) == ("Ribeirão Preto", "SP")
    # A conta antiga fica com nulo; quem mostra "não informada" é a tela.
    assert (detalhe_sem["cidade"], detalhe_sem["uf"]) == (None, None)


# --- filtro por UF -------------------------------------------------------------------------


def test_filtro_por_uf_devolve_so_as_contas_da_uf(operador, nova_conta):
    o = operador()
    sp = nova_conta(uf="SP", razao_social="Filtro Uf Paulista")
    outra_sp = nova_conta(uf="SP", razao_social="Filtro Uf Paulista Dois")
    rj = nova_conta(uf="RJ", razao_social="Filtro Uf Carioca")
    sem = nova_conta(razao_social="Filtro Uf Sem Local")

    resposta = _contas(o.headers, busca="Filtro Uf", uf="SP", limit=100)

    assert resposta.status_code == 200
    ids = {i["id"] for i in resposta.json()["itens"]}
    assert ids == {str(sp.id), str(outra_sp.id)}
    assert resposta.json()["total"] == 2
    assert str(rj.id) not in ids and str(sem.id) not in ids


def test_filtro_por_uf_inclui_a_conta_inativa_e_combina_com_a_situacao(operador, nova_conta):
    o = operador()
    ativa = nova_conta(uf="CE", razao_social="Combina Uf Ativa", piloto=True)
    inativa = nova_conta(uf="CE", razao_social="Combina Uf Inativa", inativa=True)
    nova_conta(uf="CE", razao_social="Combina Uf Sem Plano")

    so_uf = _contas(o.headers, busca="Combina Uf", uf="CE", limit=100).json()
    so_ativas = _contas(o.headers, busca="Combina Uf", uf="CE", situacao="ativo").json()
    so_inativas = _contas(o.headers, busca="Combina Uf", uf="CE", situacao="inativa").json()

    assert so_uf["total"] == 3
    assert [i["id"] for i in so_ativas["itens"]] == [str(ativa.id)]
    assert [i["id"] for i in so_inativas["itens"]] == [str(inativa.id)]


def test_filtro_nao_informada_devolve_as_contas_sem_uf(operador, nova_conta):
    o = operador()
    nova_conta(uf="BA", razao_social="Nao Informada Baiana")
    sem = nova_conta(razao_social="Nao Informada Antiga")
    so_cidade = nova_conta(cidade="Salvador", razao_social="Nao Informada So Cidade")

    resposta = _contas(o.headers, busca="Nao Informada", uf="nao_informada", limit=100)

    assert {i["id"] for i in resposta.json()["itens"]} == {str(sem.id), str(so_cidade.id)}


@pytest.mark.parametrize("uf", ["sp", "XX", "Sao Paulo", "NAO_INFORMADA"])
def test_filtro_com_uf_fora_da_lista_e_422_sem_repetir_o_valor(operador, uf):
    o = operador()

    resposta = _contas(o.headers, uf=uf)

    assert resposta.status_code == 422
    assert all("input" not in erro for erro in resposta.json()["detail"])
    assert uf not in resposta.text.replace("nao_informada", "")


def test_todas_as_ufs_sao_aceitas_no_filtro(operador):
    o = operador()

    for uf in UFS:
        assert _contas(o.headers, uf=uf, limit=1).status_code == 200, uf


# --- contagem por UF -----------------------------------------------------------------------


def test_contagem_por_uf_com_total_e_assinando(operador, nova_conta):
    o = operador()
    antes = _por_uf(o.headers).json()

    # SP: 4 contas; assinam o piloto e a Stripe `active`; `trialing` também é
    # "ativo" no painel; `past_due`, `canceled`, teste e inativa não assinam.
    nova_conta(uf="SP", piloto=True)
    nova_conta(uf="SP", stripe="active")
    nova_conta(uf="SP", stripe="past_due")
    nova_conta(uf="SP", inativa=True, piloto=True)  # inativa não assina, mesmo com piloto
    # RJ: 2 contas, uma com assinatura encerrada e uma em período de teste da Stripe.
    nova_conta(uf="RJ", stripe="canceled")
    nova_conta(uf="RJ", stripe="trialing")
    # Sem UF: 3 contas, uma assinando.
    nova_conta(piloto=True)
    nova_conta(cidade="Sem Uf")
    nova_conta()

    depois = _por_uf(o.headers)

    assert depois.status_code == 200
    corpo = depois.json()
    assert set(corpo) == {"contas", "assinando", "ufs", "nao_informada"}
    assert _grupo(corpo, "SP")["contas"] - _grupo(antes, "SP")["contas"] == 4
    assert _grupo(corpo, "SP")["assinando"] - _grupo(antes, "SP")["assinando"] == 2
    assert _grupo(corpo, "RJ")["contas"] - _grupo(antes, "RJ")["contas"] == 2
    assert _grupo(corpo, "RJ")["assinando"] - _grupo(antes, "RJ")["assinando"] == 1
    assert corpo["nao_informada"]["contas"] - antes["nao_informada"]["contas"] == 3
    assert corpo["nao_informada"]["assinando"] - antes["nao_informada"]["assinando"] == 1
    assert corpo["contas"] - antes["contas"] == 9
    assert corpo["assinando"] - antes["assinando"] == 4


def test_contagem_por_uf_fecha_com_o_total_e_com_a_lista(operador, nova_conta):
    o = operador()
    nova_conta(uf="PA", piloto=True)
    nova_conta(uf="PA")
    nova_conta()

    corpo = _por_uf(o.headers).json()

    grupos = [*corpo["ufs"], corpo["nao_informada"]]
    assert corpo["contas"] == sum(g["contas"] for g in grupos)
    assert corpo["assinando"] == sum(g["assinando"] for g in grupos)
    assert all(g["assinando"] <= g["contas"] for g in grupos)
    assert all(g["uf"] in UFS for g in corpo["ufs"])  # o "não informada" não vai na lista
    ordem = [(-g["contas"], g["uf"]) for g in corpo["ufs"]]
    assert ordem == sorted(ordem)


def test_assinando_e_a_mesma_conta_do_filtro_de_situacao(operador, nova_conta):
    o = operador()
    nova_conta(uf="AC", piloto=True, razao_social="Mesma Conta Um")
    nova_conta(uf="AC", stripe="active", razao_social="Mesma Conta Dois")
    nova_conta(uf="AC", stripe="unpaid", razao_social="Mesma Conta Tres")
    nova_conta(uf="AC", inativa=True, piloto=True, razao_social="Mesma Conta Quatro")

    do_painel = _contas(o.headers, uf="AC", situacao="ativo", limit=100).json()["total"]
    contagem = _grupo(_por_uf(o.headers).json(), "AC")

    assert contagem["assinando"] == do_painel


# --- só operador ---------------------------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_quem_nao_e_operador_recebe_403_na_lista_filtrada_e_na_contagem(cliente, papel):
    c = cliente()

    for rota in (*ROTAS_DE_LEITURA, "/admin/contas?uf=SP"):
        resposta = client.get(rota, headers=c.headers[papel])
        assert (resposta.status_code, resposta.json()) == (403, SO_OPERADOR), rota


def test_sem_login_e_401_na_contagem():
    assert client.get("/admin/contas/por-uf").status_code == 401


def test_a_rota_da_contagem_nao_e_confundida_com_o_detalhe(operador):
    o = operador()

    assert _por_uf(o.headers).status_code == 200
    assert client.get(f"/admin/contas/{uuid.uuid4()}", headers=o.headers).status_code == 404
