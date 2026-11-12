"""As superfícies que conhecem o `match_agrupado` (issue #131, ADR-027): a
listagem de `GET /conciliacoes/{id}` com `grupo_id`, o resumo `grupo` e o filtro
por grupo, a regra de decisão, a exportação CSV, `GET /execucoes`, o fechamento
e o resumo congelado, o relatório público, a API pública `/v1` e o contexto da
IA. O MCP está em tests/test_mcp_servidor.py. O motor e o que ele grava, em
tests/test_matching_agrupamento.py e tests/test_conciliacoes_agrupamento.py.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import event
from test_conciliacoes_agrupamento import LOTE_DE_150_MIL, inserir, lote

from app.core.config import settings
from app.core.database import engine
from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Fechamento
from app.services import decisoes
from app.services.ia.contexto import montar_contexto_divergencia
from app.services.ia.prompt import motivo_deterministico
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
DIA = date(2026, 9, 5)


@pytest.fixture(autouse=True)
def _flags_ligadas(monkeypatch):
    monkeypatch.setattr(settings, "agrupamento_habilitado", True)
    monkeypatch.setattr(settings, "link_publico_habilitado", True)


@pytest.fixture
def conta(db_session, criar_usuario, criar_extrato_da_empresa, gerar_token):
    """Admin de uma conta com uma empresa e uma fábrica de pares conciliados
    em setembro de 2026, pela API."""
    usuario = criar_usuario()
    empresa_id = empresa_unica(db_session, usuario.conta_id)
    headers = cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id), empresa_id)

    def par(itens_banco, itens_sistema, tolerancia=None):
        if tolerancia is not None:
            db_session.add(Configuracao(empresa_id=empresa_id, tolerancia_dias_default=tolerancia))
            db_session.commit()
        extratos = {}
        for origem, itens in (("banco", itens_banco), ("sistema", itens_sistema)):
            novo = criar_extrato_da_empresa(empresa_id, origem)
            inserir(db_session, novo, itens)
            novo.periodo_inicio, novo.periodo_fim = SETEMBRO
            db_session.commit()
            extratos[origem] = novo
        resposta = client.post(
            "/conciliacoes",
            headers=headers,
            json={
                "extrato_banco_id": str(extratos["banco"].id),
                "extrato_sistema_id": str(extratos["sistema"].id),
            },
        )
        assert resposta.status_code == 201, resposta.text
        return extratos["banco"], extratos["sistema"]

    def itens(banco, **params):
        resposta = client.get(f"/conciliacoes/{banco.id}", headers=headers, params=params)
        assert resposta.status_code == 200, resposta.text
        return resposta.json()

    return SimpleNamespace(
        usuario=usuario, empresa_id=empresa_id, headers=headers, par=par, itens=itens
    )


DESVIOS_EM_DIAS = [2, 0, 0, -1, 1, 0, 0, -1, 1, 0]  # o maior é de 2 dias, a tolerância da empresa


@pytest.fixture
def lote_de_setembro(conta):
    """O lote de 150 mil (1 do banco contra 10 do sistema, espalhados em até 2 dias,
    com tolerância 2), uma linha só do banco e uma só do sistema."""
    sistema = [
        (f"-{valor}.00", DIA + timedelta(days=desvio), f"P{indice}")
        for indice, (valor, desvio) in enumerate(zip(LOTE_DE_150_MIL, DESVIOS_EM_DIAS, strict=True))
    ]
    banco, sistema_extrato = conta.par(
        [
            ("-150000.00", DIA, "Pagamento em lote"),
            ("-20.00", DIA + timedelta(days=9), "sobra do banco"),
        ],
        sistema + [("-3.33", DIA + timedelta(days=20), "sobra do sistema")],
        tolerancia=2,
    )
    return SimpleNamespace(banco=banco, sistema=sistema_extrato)


def justificar_o_que_sobrou(conta, banco):
    """Justifica as divergências (as agrupadas não aceitam decisão): o mês fica pronto."""
    for item in conta.itens(banco)["itens"]:
        if item["status"] != "match_agrupado":
            resposta = client.post(
                f"/conciliacoes/{banco.id}/decisoes",
                headers=conta.headers,
                json={"chave": item["chave"], "tipo": "justificada", "texto": "motivo"},
            )
            assert resposta.status_code == 200, resposta.text


# --- listagem de GET /conciliacoes/{id}


def test_listagem_traz_grupo_id_e_o_resumo_do_grupo(conta, lote_de_setembro):
    corpo = conta.itens(lote_de_setembro.banco)

    agrupados = [i for i in corpo["itens"] if i["status"] == "match_agrupado"]
    outros = [i for i in corpo["itens"] if i["status"] != "match_agrupado"]
    assert len(agrupados) == 11 and len(outros) == 2
    assert len({i["grupo_id"] for i in agrupados}) == 1 and agrupados[0]["grupo_id"] is not None
    for item in agrupados:
        assert item["regra_aplicada"] == "agrupamento"
        assert item["grupo"] == {
            "quantidade_banco": 1,
            "quantidade_sistema": 10,
            "valor_unico": "-150000.00",
            "maior_desvio_dias": 2,
        }
        assert (item["lancamento_banco"] is None) != (item["lancamento_sistema"] is None)
        assert item["decisao"] is None and item["eventos"] == []
    for item in outros:
        assert item["grupo_id"] is None and item["grupo"] is None


def test_resumo_do_grupo_com_o_unico_do_sistema(conta):
    banco, _ = conta.par(
        [("-1000.00", DIA, "a"), ("-2000.00", DIA, "b"), ("-3000.00", DIA, "c")],
        [("-6000.00", DIA, "fornecedor")],
    )

    (grupo,) = {
        i["grupo_id"]: i["grupo"] for i in conta.itens(banco)["itens"] if i["grupo"]
    }.values()

    assert grupo == {
        "quantidade_banco": 3,
        "quantidade_sistema": 1,
        "valor_unico": "-6000.00",
        "maior_desvio_dias": 0,
    }


def test_filtro_por_status_e_por_grupo_id_atravessa_paginas(conta):
    # um lote de 200 lançamentos atravessa as páginas; o resumo vale pro grupo inteiro
    banco, _ = conta.par(
        [("-200000.00", DIA, "lote"), ("-7.00", DIA + timedelta(days=9), "sobra")],
        [("-1000.00", DIA, f"parcela {i}") for i in range(200)],
    )
    todos = conta.itens(banco, status="match_agrupado", limit=1000)
    assert todos["total"] == 201
    grupo_id = todos["itens"][0]["grupo_id"]

    vistos, offset = [], 0
    while True:
        pagina = conta.itens(banco, grupo_id=grupo_id, limit=50, offset=offset)
        assert pagina["total"] == 201
        for item in pagina["itens"]:
            assert item["grupo"]["quantidade_sistema"] == 200  # o grupo inteiro, não a página
        vistos += [i["id"] for i in pagina["itens"]]
        offset += 50
        if offset >= pagina["total"]:
            break
    assert len(vistos) == len(set(vistos)) == 201

    assert conta.itens(banco, status="sem_correspondencia")["total"] == 1
    assert conta.itens(banco, grupo_id="00000000-0000-0000-0000-000000000000")["total"] == 0


def test_grupo_id_de_outra_empresa_nao_aparece(
    conta, criar_usuario, criar_extrato_da_empresa, gerar_token, db_session, lote_de_setembro
):
    grupo_id = next(
        i["grupo_id"] for i in conta.itens(lote_de_setembro.banco)["itens"] if i["grupo_id"]
    )
    outro = criar_usuario()
    outra_empresa = empresa_unica(db_session, outro.conta_id)
    extrato = criar_extrato_da_empresa(outra_empresa, "banco")
    outros_headers = cabecalhos(gerar_token(outro.conta_id, usuario_id=outro.id), outra_empresa)

    resposta = client.get(
        f"/conciliacoes/{extrato.id}", headers=outros_headers, params={"grupo_id": grupo_id}
    )
    assert resposta.status_code == 200 and resposta.json()["total"] == 0
    # e o extrato do banco de outra empresa continua sendo 404
    assert (
        client.get(f"/conciliacoes/{lote_de_setembro.banco.id}", headers=outros_headers).status_code
        == 404
    )


def test_resumo_do_grupo_sai_de_uma_consulta_agregada_por_pagina(conta, lote_de_setembro):
    cabecalho = conta.headers

    def selects_da_chamada(limit):
        selects: list[str] = []

        def contar(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(engine, "before_cursor_execute", contar)
        try:
            resposta = client.get(
                f"/conciliacoes/{lote_de_setembro.banco.id}",
                headers=cabecalho,
                params={"limit": limit},
            )
        finally:
            event.remove(engine, "before_cursor_execute", contar)
        assert resposta.status_code == 200
        return len(selects), resposta.json()["itens"]

    selects_pequena, itens_pequena = selects_da_chamada(3)
    selects_cheia, itens_cheia = selects_da_chamada(13)

    assert any(i["grupo"] for i in itens_pequena)
    assert sum(bool(i["grupo"]) for i in itens_cheia) == 11
    # não cresce com a página: o resumo é uma consulta só, não uma por linha
    # (9 da listagem sem grupo, ver tests/test_conciliacoes.py, mais a dos grupos)
    assert selects_pequena == selects_cheia <= 10


# --- decisão


def test_linha_agrupada_nao_aceita_decisao(conta, lote_de_setembro):
    itens = conta.itens(lote_de_setembro.banco)["itens"]
    do_banco = next(i for i in itens if i["status"] == "match_agrupado" and i["lancamento_banco"])
    do_sistema = next(
        i for i in itens if i["status"] == "match_agrupado" and i["lancamento_sistema"]
    )

    for item in (do_banco, do_sistema):
        for tipo, texto in (("conferida", None), ("justificada", "motivo")):
            resposta = client.post(
                f"/conciliacoes/{lote_de_setembro.banco.id}/decisoes",
                headers=conta.headers,
                json={"chave": item["chave"], "tipo": tipo, "texto": texto},
            )
            assert resposta.status_code == 422
            assert resposta.json()["detail"] == "Só é possível decidir sobre uma linha que diverge."

    assert decisoes.diverge("match_agrupado") is False
    assert decisoes.diverge("match_exato") is False
    assert decisoes.diverge("sem_correspondencia") is True


def test_a_chave_da_linha_agrupada_e_a_de_sempre(conta, lote_de_setembro):
    itens = conta.itens(lote_de_setembro.banco)["itens"]
    do_banco = next(i for i in itens if i["status"] == "match_agrupado" and i["lancamento_banco"])
    do_sistema = next(
        i for i in itens if i["status"] == "match_agrupado" and i["lancamento_sistema"]
    )

    assert do_banco["chave"] == do_banco["lancamento_banco"]["id"]
    assert len(do_sistema["chave"]) == 64  # sha256 da linha só do sistema


# --- exportação CSV


def _csv(resposta):
    texto = resposta.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(texto), delimiter=";"))


def test_csv_tem_o_rotulo_e_a_coluna_grupo_numerada_na_ordem_do_arquivo(conta):
    # dois grupos, um por sentido, e uma linha sem par
    banco, _ = conta.par(
        [("-150000.00", DIA, "lote")]
        + [(f"-{v}.00", DIA + timedelta(days=1), f"l{v}") for v in (1000, 2000, 3000)]
        + [("-9.99", DIA + timedelta(days=9), "sobra")],
        lote(LOTE_DE_150_MIL) + [("-6000.00", DIA + timedelta(days=1), "fornecedor")],
    )

    resposta = client.get(f"/conciliacoes/{banco.id}/exportar", headers=conta.headers)

    assert resposta.status_code == 200
    linhas = _csv(resposta)
    cabecalho, corpo = linhas[0], linhas[1:]
    assert cabecalho[-1] == "Grupo" and len(cabecalho) == 10
    assert cabecalho[:9] == [
        "Status", "Data (banco)", "Valor (banco)", "Descrição (banco)", "Data (sistema)",
        "Valor (sistema)", "Descrição (sistema)", "Regra aplicada", "Score de confiança",
    ]  # fmt: skip
    agrupadas = [linha for linha in corpo if linha[0] == "Conciliado por agrupamento"]
    assert len(agrupadas) == 15
    assert {linha[7] for linha in agrupadas} == {"Agrupamento por soma"}
    assert {linha[8] for linha in agrupadas} == {"0,900"}
    assert {linha[-1] for linha in agrupadas} == {"1", "2"}
    assert (
        sum(linha[-1] == "1" for linha in agrupadas) + sum(linha[-1] == "2" for linha in agrupadas)
        == 15
    )
    # numeração na ordem do arquivo: o primeiro grupo a aparecer é o 1
    assert next(linha[-1] for linha in agrupadas) == "1"
    # as demais linhas ficam com a coluna vazia
    assert [linha[-1] for linha in corpo if linha[0] != "Conciliado por agrupamento"] == [""]


def test_csv_sem_linha_agrupada_e_identico_ao_de_antes(conta):
    banco, _ = conta.par([("-10.00", DIA, "a"), ("-20.00", DIA, "b")], [("-10.00", DIA, "a")])

    linhas = _csv(client.get(f"/conciliacoes/{banco.id}/exportar", headers=conta.headers))

    assert len(linhas[0]) == 9 and "Grupo" not in linhas[0]
    assert all(len(linha) == 9 for linha in linhas)


def test_csv_exporta_so_o_grupo_filtrado(conta, lote_de_setembro):
    grupo_id = next(
        i["grupo_id"] for i in conta.itens(lote_de_setembro.banco)["itens"] if i["grupo_id"]
    )

    linhas = _csv(
        client.get(
            f"/conciliacoes/{lote_de_setembro.banco.id}/exportar",
            headers=conta.headers,
            params={"grupo_id": grupo_id},
        )
    )

    assert len(linhas) == 12 and {linha[-1] for linha in linhas[1:]} == {"1"}


# --- GET /execucoes


def test_execucoes_trazem_as_contagens_novas_e_o_percentual_conta_o_agrupado(
    conta, lote_de_setembro
):
    resposta = client.get("/execucoes", headers=conta.headers)

    (execucao,) = resposta.json()["itens"]
    contagens = execucao["contagens"]
    assert contagens["match_agrupado"] == 11 and contagens["agrupamentos"] == 1
    assert contagens["total"] == 13
    assert execucao["percentual_acerto"] == "84.62"  # 11 / 13


def test_execucao_anterior_ao_agrupamento_devolve_zero(db_session, conta, criar_extrato_da_empresa):
    banco = criar_extrato_da_empresa(conta.empresa_id, "banco")
    sistema = criar_extrato_da_empresa(conta.empresa_id, "sistema")
    db_session.add(
        ExecucaoConciliacao(
            empresa_id=conta.empresa_id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            tolerancia_dias=0,
            total=2,
            match_exato=1,
            match_tolerancia=0,
            duplicado=0,
            sem_correspondencia=1,
            tarifa_bancaria=0,
            divergente_valor=0,
            divergente_data=0,
        )
    )
    db_session.commit()

    (execucao,) = client.get("/execucoes", headers=conta.headers).json()["itens"]

    assert execucao["contagens"]["match_agrupado"] == 0
    assert execucao["contagens"]["agrupamentos"] == 0
    assert execucao["percentual_acerto"] == "50.00"


# --- fechamento


def test_fechamento_congela_o_agrupamento_e_o_lote_nao_deixa_pendencia(conta, lote_de_setembro):
    justificar_o_que_sobrou(conta, lote_de_setembro.banco)

    resposta = client.post("/fechamentos", headers=conta.headers, json={"competencia": "2026-09"})

    assert resposta.status_code == 201, resposta.text
    resumo = resposta.json()["resumo"]
    assert resumo["contagens"]["match_agrupado"] == 11 and resumo["contagens"]["agrupamentos"] == 1
    assert resumo["contagens"]["total"] == 13
    assert resumo["justificadas"] == 2 and resumo["pendentes"] == 0  # as 11 agrupadas não contam


def test_fechamento_antigo_sem_as_chaves_le_como_zero(db_session, conta, lote_de_setembro):
    justificar_o_que_sobrou(conta, lote_de_setembro.banco)
    fechado = client.post("/fechamentos", headers=conta.headers, json={"competencia": "2026-09"})
    assert fechado.status_code == 201, fechado.text

    # o resumo gravado antes da #131 não tem as duas chaves, e nenhuma migração de dados as põe
    fechamento = db_session.query(Fechamento).filter_by(empresa_id=conta.empresa_id).one()
    contagens = {**fechamento.resumo["contagens"], "total": 2}
    del contagens["match_agrupado"], contagens["agrupamentos"]
    fechamento.resumo = {**fechamento.resumo, "contagens": contagens}
    db_session.commit()

    (item,) = client.get("/fechamentos", headers=conta.headers).json()["itens"]
    assert item["resumo"]["contagens"]["match_agrupado"] == 0
    assert item["resumo"]["contagens"]["agrupamentos"] == 0
    token = client.post("/fechamentos/2026-09/links", headers=conta.headers).json()["token"]
    publico = client.post("/relatorios-publicos/consultar", json={"token": token}).json()
    assert publico["resumo"]["conciliadas"] == 0 and publico["resumo"]["agrupamentos"] == 0


def test_relatorio_publico_soma_o_agrupado_em_conciliadas_e_mostra_os_grupos(
    conta, lote_de_setembro
):
    justificar_o_que_sobrou(conta, lote_de_setembro.banco)
    fechado = client.post("/fechamentos", headers=conta.headers, json={"competencia": "2026-09"})
    assert fechado.status_code == 201, fechado.text
    token = client.post("/fechamentos/2026-09/links", headers=conta.headers).json()["token"]

    corpo = client.post("/relatorios-publicos/consultar", json={"token": token}).json()

    assert corpo["resumo"]["conciliadas"] == 11
    assert corpo["resumo"]["agrupamentos"] == 1
    assert corpo["resumo"]["total"] == 13
    # linha conciliada não aparece no relatório: só as divergentes
    (par,) = corpo["pares"]
    assert len(par["justificadas"]) + len(par["pendentes"]) == 2


# --- API pública /v1


def test_v1_traz_match_agrupado_e_agrupamentos_sem_expor_descricao(conta, lote_de_setembro):
    chave = client.post("/chaves-api", headers=conta.headers, json={"nome": "Integração"}).json()[
        "chave"
    ]
    cabecalho = {"Authorization": f"Bearer {chave}"}
    base = f"/v1/empresas/{conta.empresa_id}"

    resumo = client.get(
        f"{base}/resumo", headers=cabecalho, params={"competencia": "2026-09"}
    ).json()
    assert resumo["contagens"]["match_agrupado"] == 11
    assert resumo["contagens"]["agrupamentos"] == 1
    assert resumo["contagens"]["total"] == 13

    pares = client.get(
        f"{base}/conciliacoes", headers=cabecalho, params={"competencia": "2026-09"}
    ).json()
    assert pares["itens"][0]["contagens"]["match_agrupado"] == 11
    assert pares["itens"][0]["contagens"]["agrupamentos"] == 1

    divergencias = client.get(
        f"{base}/divergencias", headers=cabecalho, params={"competencia": "2026-09"}
    )
    assert divergencias.json()["total"] == 2  # as agrupadas nunca são divergência
    assert "Pagamento em lote" not in divergencias.text and "sobra" not in divergencias.text


# --- IA


def test_match_agrupado_nunca_e_candidato_nem_divergencia_para_a_ia(db_session, conta):
    banco, _ = conta.par(
        [("-150000.00", DIA, "lote"), ("-1.23", DIA, "sobra do banco")],
        lote(LOTE_DE_150_MIL) + [("-9.99", DIA, "sobra do sistema")],
    )
    db_session.expire_all()
    linhas = db_session.query(Conciliacao).filter_by(extrato_banco_id=banco.id).all()
    agrupada = next(linha for linha in linhas if linha.status == "match_agrupado")
    divergente = next(
        linha
        for linha in linhas
        if linha.status == "divergente_valor" and linha.lancamento_banco_id
    )

    with pytest.raises(ValueError):
        montar_contexto_divergencia(db_session, agrupada)
    with pytest.raises(ValueError):
        motivo_deterministico("match_agrupado")

    contexto = montar_contexto_divergencia(db_session, divergente)
    # só o -9.99 do sistema: os 10 lançamentos do lote já têm grupo e não são candidatos
    assert [c.lancamento.valor for c in contexto.candidatos] == [Decimal("-9.99")]


def test_explicacao_de_linha_agrupada_e_422(conta, lote_de_setembro, db_session):
    db_session.expire_all()
    agrupada = (
        db_session.query(Conciliacao)
        .filter_by(extrato_banco_id=lote_de_setembro.banco.id, status="match_agrupado")
        .first()
    )

    resposta = client.post(
        "/explicacoes", headers=conta.headers, json={"conciliacao_id": str(agrupada.id)}
    )

    assert resposta.status_code == 422
