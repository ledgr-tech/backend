# ruff: noqa: F811
"""Ferramentas do PR 2 da issue #225: lançamentos do mês, comparação entre meses,
grupos do mês, e os prompts do MCP.

Mesmo padrão de tests/test_mcp_servidor.py e do PR 1
(tests/test_mcp_ferramentas_ampliadas.py): cliente do SDK em processo, token do
fluxo OAuth, `cenario` (conta com duas empresas, A e B, cada uma com setembro
conciliado) e `outubro` (A com outubro de 2026, montado à mão). Aqui A ganha,
conforme o teste, novembro (tudo bate) e dezembro, com um lote conciliado por
agrupamento (um lançamento de R$ 150.000,00 do banco contra dez do sistema).
"""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from conftest import cabecalhos
from test_mcp_ferramentas_ampliadas import (  # noqa: F401 - `outubro` é fixture
    ARQUIVO_SECRETO,
    DESCRICAO_SECRETA,
    DESCRICAO_TARIFA,
    FERRAMENTAS_VARRIDAS_NO_PR_1,
    _lancamentos,
    outubro,
)
from test_mcp_oauth import mcp  # noqa: F401 - fixture usada com usefixtures
from test_mcp_servidor import _chamar, _com_cliente, _texto, cenario  # noqa: F401

from app.core.config import settings

PARCELAS = (5000, 8000, 11000, 12500, 14000, 15500, 17000, 18500, 21000, 27500)


@pytest.fixture
def mes(cenario, db_session, criar_extrato_da_empresa):
    """`mes(competencia, banco, sistema)` concilia, pela API, um mês da empresa A. Os itens
    são (data, valor, descrição)."""

    def _semear(ano_mes: str, banco, sistema, empresa_id=None, saldo=None):
        empresa_id = empresa_id or cenario.empresa_a
        ano, numero = (int(p) for p in ano_mes.split("-"))
        extratos = {}
        for origem, itens in (("banco", banco), ("sistema", sistema)):
            extrato = criar_extrato_da_empresa(empresa_id, origem)
            extrato.nome_arquivo = ARQUIVO_SECRETO
            extrato.periodo_inicio, extrato.periodo_fim = date(ano, numero, 1), date(
                ano, numero, 28
            )
            db_session.commit()
            _lancamentos(db_session, extrato, itens)
            extratos[origem] = extrato
        resposta = cenario.http.post(
            "/conciliacoes",
            headers=cabecalhos(cenario.jwt, empresa_id),
            json={
                "extrato_banco_id": str(extratos["banco"].id),
                "extrato_sistema_id": str(extratos["sistema"].id),
            },
        )
        assert resposta.status_code == 201, resposta.text
        return resposta.json()

    return _semear


def _novembro(mes) -> None:
    """Novembro de A: dois pares exatos, nenhuma divergência."""
    itens = [(date(2026, 11, 3), "40.00", "NOV a"), (date(2026, 11, 4), "-60.00", "NOV b")]
    mes("2026-11", itens, itens)


def _lote(mes, monkeypatch, ano_mes: str, lado_unico: str) -> dict:
    """Um lote de R$ 150.000,00: um lançamento de um lado contra dez parcelas do outro."""
    monkeypatch.setattr(settings, "agrupamento_habilitado", True)
    ano, numero = (int(p) for p in ano_mes.split("-"))
    unico = [(date(ano, numero, 20), "-150000.00", f"{DESCRICAO_SECRETA} lote")]
    parcelas = [
        (date(ano, numero, 20), f"-{valor}.00", f"{DESCRICAO_SECRETA} parcela {i}")
        for i, valor in enumerate(PARCELAS)
    ]
    banco, sistema = (unico, parcelas) if lado_unico == "banco" else (parcelas, unico)
    resposta = mes(ano_mes, banco, sistema)
    assert resposta["match_agrupado"] == 11 and resposta["agrupamentos"] == 1
    return resposta


def _lancamentos_do_mes(token: str, **argumentos) -> dict:
    resultado = _chamar(token, "lancamentos_do_mes", argumentos)
    assert resultado.is_error is not True, _texto(resultado)
    return resultado.structured_content


def _por_valor(lancamentos: dict, valor: str, lado: str | None = None) -> list[dict]:
    return [
        i
        for i in lancamentos["itens"]
        if i["valor"] == valor and (lado is None or i["lado"] == lado)
    ]


# --- lançamentos do mês ----------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_lancamentos_do_mes_traz_lado_situacao_e_o_id_da_divergencia(outubro):
    outubro.outubro()
    token = outubro.conectar()

    lancamentos = _lancamentos_do_mes(token, competencia="2026-10")
    divergencias = _chamar(token, "divergencias_do_mes", {"competencia": "2026-10"})

    assert lancamentos["total"] == 7 and lancamentos["truncado"] is False
    assert {i["lado"] for i in lancamentos["itens"]} == {"banco", "sistema"}
    assert sum(i["lado"] == "banco" for i in lancamentos["itens"]) == 5
    assert {i["situacao"] for i in _por_valor(lancamentos, "10.00")} == {"conciliado"}
    assert all(i["divergencia_id"] is None for i in _por_valor(lancamentos, "10.00"))
    assert [i["situacao"] for i in _por_valor(lancamentos, "-1500.00")] == ["sem_correspondencia"]
    assert [i["situacao"] for i in _por_valor(lancamentos, "-25.00")] == ["tarifa_bancaria"]
    # O par de divergente_data tem uma linha só: os dois lançamentos apontam para a mesma divergência.
    cinquenta = _por_valor(lancamentos, "50.00")
    assert {i["lado"] for i in cinquenta} == {"banco", "sistema"}
    assert {i["situacao"] for i in cinquenta} == {"divergente_data"}
    assert len({i["divergencia_id"] for i in cinquenta}) == 1
    # E todo `divergencia_id` é um `id` de divergencias_do_mes.
    ids = {i["id"] for i in divergencias.structured_content["itens"]}
    assert {i["divergencia_id"] for i in lancamentos["itens"] if i["divergencia_id"]} == ids
    detalhe = _chamar(token, "detalhe_da_divergencia", {"id": cinquenta[0]["divergencia_id"]})
    assert detalhe.structured_content["categoria"] == "divergente_data"
    assert {"id", "lado", "data", "valor", "tipo", "situacao", "divergencia_id"} == set(
        lancamentos["itens"][0]
    )


@pytest.mark.usefixtures("mcp")
def test_pergunta_12_existe_um_lancamento_deste_valor_e_foi_conciliado(outubro):
    outubro.outubro()
    token = outubro.conectar()

    existe = _lancamentos_do_mes(token, valor_minimo=300, valor_maximo=300)
    nao_existe = _lancamentos_do_mes(token, valor_minimo="1250", valor_maximo="1250")

    assert [(i["valor"], i["situacao"]) for i in existe["itens"]] == [
        ("300.00", "sem_correspondencia")
    ]
    assert nao_existe["total"] == 0 and nao_existe["itens"] == []


@pytest.mark.usefixtures("mcp")
def test_pergunta_13_lancamentos_do_banco_no_dia_sem_correspondencia(outubro):
    outubro.outubro()
    token = outubro.conectar()

    dia = _lancamentos_do_mes(
        token, lado="banco", data="2026-10-12", situacao="sem_correspondencia"
    )
    outro_dia = _lancamentos_do_mes(token, lado="banco", data="2026-10-11")

    assert [(i["data"], i["valor"]) for i in dia["itens"]] == [("2026-10-12", "300.00")]
    assert outro_dia["total"] == 0


@pytest.mark.usefixtures("mcp")
def test_pergunta_14_debitos_acima_de_um_valor(outubro):
    outubro.outubro()
    token = outubro.conectar()

    debitos = _lancamentos_do_mes(token, tipo="debito", valor_minimo=1000)
    todos = _lancamentos_do_mes(token, tipo="debito", ordenar_por="valor", ordem="desc")
    creditos = _lancamentos_do_mes(token, tipo="credito", lado="sistema")

    assert [i["valor"] for i in debitos["itens"]] == ["-1500.00"]
    assert [i["valor"] for i in todos["itens"]] == ["-1500.00", "-25.00"]
    assert {i["tipo"] for i in creditos["itens"]} == {"credito"} and creditos["total"] == 2


@pytest.mark.usefixtures("mcp")
def test_filtros_de_situacao_lado_e_intervalo_de_datas(outubro):
    outubro.outubro()
    token = outubro.conectar()

    conciliados = _lancamentos_do_mes(token, situacao="conciliado")
    tarifas = _lancamentos_do_mes(token, situacao="tarifa_bancaria")
    do_sistema = _lancamentos_do_mes(token, lado="sistema")
    primeira_semana = _lancamentos_do_mes(token, data_inicial="2026-10-01", data_final="2026-10-07")
    so_inicial = _lancamentos_do_mes(token, data_inicial="2026-10-12")
    so_final = _lancamentos_do_mes(token, data_final="2026-10-01")

    assert conciliados["total"] == 2  # o 10,00 do banco e o do sistema
    assert [i["valor"] for i in tarifas["itens"]] == ["-25.00"]
    assert do_sistema["total"] == 2 and {i["lado"] for i in do_sistema["itens"]} == {"sistema"}
    assert sorted(i["valor"] for i in primeira_semana["itens"]) == [
        "10.00",
        "10.00",
        "50.00",
        "50.00",
    ]
    assert sorted(i["data"] for i in so_inicial["itens"]) == ["2026-10-12", "2026-10-15"]
    assert sorted(i["valor"] for i in so_final["itens"]) == ["10.00", "10.00"]


@pytest.mark.usefixtures("mcp")
def test_lancamentos_ordenacao_e_paginacao(outubro):
    outubro.outubro()
    token = outubro.conectar()

    por_data = _lancamentos_do_mes(token, lado="banco")
    por_data_desc = _lancamentos_do_mes(token, lado="banco", ordem="desc")
    por_valor = _lancamentos_do_mes(token, lado="banco", ordenar_por="valor", ordem="desc")
    primeira = _lancamentos_do_mes(token, lado="banco", limite=2)
    ultima = _lancamentos_do_mes(token, lado="banco", limite=2, deslocamento=4)

    assert [i["data"] for i in por_data["itens"]] == [
        "2026-10-01",
        "2026-10-05",
        "2026-10-10",
        "2026-10-12",
        "2026-10-15",
    ]
    assert [i["data"] for i in por_data_desc["itens"]] == [
        i["data"] for i in reversed(por_data["itens"])
    ]
    assert [i["valor"] for i in por_valor["itens"]] == [
        "-1500.00",
        "300.00",
        "50.00",
        "-25.00",
        "10.00",
    ]
    assert len(primeira["itens"]) == 2 and primeira["total"] == 5 and primeira["truncado"] is True
    assert len(ultima["itens"]) == 1 and ultima["truncado"] is False
    assert [i["id"] for i in primeira["itens"]] == [i["id"] for i in por_data["itens"][:2]]
    assert "Há mais páginas" in _texto(_chamar(token, "lancamentos_do_mes", {"limite": 2}))


@pytest.mark.usefixtures("mcp")
def test_lancamentos_competencia_omitida_e_a_mais_recente(outubro):
    outubro.outubro()

    resultado = _chamar(outubro.conectar(), "lancamentos_do_mes", {})

    assert resultado.structured_content["competencia"] == "2026-10"
    assert _texto(resultado).startswith("Outubro de 2026: 7 lançamentos")


@pytest.mark.usefixtures("mcp")
def test_lancamentos_nunca_trazem_id_nem_dado_da_outra_empresa(outubro):
    outubro.outubro()
    de_a = _lancamentos_do_mes(outubro.conectar(), competencia="2026-10")
    da_b = _lancamentos_do_mes(outubro.conectar(empresa_id=outubro.empresa_b))

    assert da_b["competencia"] == "2026-09" and da_b["total"] == 6
    ids_de_a = {i["id"] for i in de_a["itens"]} | {
        i["divergencia_id"] for i in de_a["itens"] if i["divergencia_id"]
    }
    assert not ids_de_a & (
        {i["id"] for i in da_b["itens"]} | {i["divergencia_id"] for i in da_b["itens"]}
    )
    assert "-1500.00" not in json.dumps(da_b)
    token_b = outubro.conectar(empresa_id=outubro.empresa_b)
    # O mês de A, pedido pela conexão de B, é um mês sem nada na B.
    assert _lancamentos_do_mes(token_b, competencia="2026-10")["total"] == 0


@pytest.mark.parametrize(
    ("argumentos", "trecho"),
    [
        ({"competencia": "2026-13"}, "Competência inválida"),
        ({"limite": 0}, "limite vai de 1 a 200"),
        ({"limite": 201}, "limite vai de 1 a 200"),
        ({"deslocamento": -1}, "deslocamento"),
        ({"valor_minimo": -1}, "não podem ser negativos"),
        ({"valor_minimo": 10, "valor_maximo": 5}, "mínimo não pode ser maior"),
        ({"situacao": "x"}, "Input should be 'conciliado'"),
        ({"lado": "ambos"}, "Input should be 'banco' or 'sistema'"),
        ({"tipo": "pix"}, "Input should be 'credito' or 'debito'"),
        ({"ordenar_por": "nome"}, "Input should be 'data' or 'valor'"),
        ({"data": "12/10/2026"}, "data"),
        ({"data": "2026-10-12", "data_inicial": "2026-10-01"}, "não os dois"),
        ({"data_inicial": "2026-10-20", "data_final": "2026-10-01"}, "não pode ser depois"),
    ],
)
@pytest.mark.usefixtures("mcp")
def test_lancamentos_entrada_invalida_e_erro_da_ferramenta(
    cenario,
    argumentos,
    trecho,
):
    resultado = _chamar(cenario.conectar(), "lancamentos_do_mes", argumentos)

    assert resultado.is_error is True
    assert trecho in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_lancamentos_de_empresa_sem_conciliacao_orienta(cenario, criar_empresa):
    vazia = criar_empresa(razao_social="Empresa Vazia Ltda", conta_id=cenario.admin.conta_id).id

    resultado = _chamar(cenario.conectar(empresa_id=vazia), "lancamentos_do_mes", {})

    assert resultado.is_error is True and "ainda não tem conciliação" in _texto(resultado)


# --- comparar meses --------------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_comparar_meses_traz_a_serie_com_a_variacao_para_o_mes_anterior(outubro, mes):
    outubro.outubro()
    _novembro(mes)

    resultado = _chamar(outubro.conectar(), "comparar_meses", {})

    serie = resultado.structured_content
    assert serie["quantidade"] == 3
    assert [i["competencia"] for i in serie["itens"]] == ["2026-09", "2026-10", "2026-11"]
    setembro, outubro_, novembro = serie["itens"]
    assert setembro["variacao"] is None and setembro["variacao_percentual"] is None
    assert setembro["fechado"] is True and outubro_["fechado"] is False
    assert setembro["divergencias"] == {
        "total": 4,
        "divergente_valor": 4,
        "divergente_data": 0,
        "sem_correspondencia": 0,
        "duplicado": 0,
        "tarifa_bancaria": 0,
    }
    assert (setembro["linhas"], setembro["conciliadas"]) == (5, 1)
    assert outubro_["divergencias"] == {
        "total": 4,
        "divergente_valor": 0,
        "divergente_data": 1,
        "sem_correspondencia": 2,
        "duplicado": 0,
        "tarifa_bancaria": 1,
    }
    assert outubro_["variacao"] == {
        "total": 0,
        "divergente_valor": -4,
        "divergente_data": 1,
        "sem_correspondencia": 2,
        "duplicado": 0,
        "tarifa_bancaria": 1,
    }
    assert outubro_["variacao_percentual"] == "0.00"
    assert novembro["divergencias"]["total"] == 0 and (
        novembro["linhas"],
        novembro["conciliadas"],
    ) == (2, 2)
    assert novembro["variacao"]["total"] == -4 and novembro["variacao_percentual"] == "-100.00"
    assert _texto(resultado) == (
        "3 meses com conciliação, de setembro de 2026 a novembro de 2026: 0 divergências no "
        "último, 4 a menos que no mês anterior."
    )


@pytest.mark.usefixtures("mcp")
def test_pergunta_4_outubro_contra_setembro(outubro):
    outubro.outubro()

    resultado = _chamar(outubro.conectar(), "comparar_meses", {"quantidade": 2, "ate": "2026-10"})

    assert [i["competencia"] for i in resultado.structured_content["itens"]] == [
        "2026-09",
        "2026-10",
    ]
    assert "igual ao mês anterior" in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_comparar_meses_corta_na_quantidade_e_no_ate(outubro, mes):
    outubro.outubro()
    _novembro(mes)
    token = outubro.conectar()

    ultimo = _chamar(token, "comparar_meses", {"quantidade": 1}).structured_content
    ate_setembro = _chamar(token, "comparar_meses", {"ate": "2026-09"}).structured_content
    seis = _chamar(token, "comparar_meses", {"quantidade": 6}).structured_content

    assert [i["competencia"] for i in ultimo["itens"]] == ["2026-11"]
    assert ultimo["itens"][0]["variacao"] is None
    assert [i["competencia"] for i in ate_setembro["itens"]] == ["2026-09"]
    assert seis["quantidade"] == 3


@pytest.mark.usefixtures("mcp")
def test_comparar_meses_so_traz_a_empresa_da_conexao(outubro):
    outubro.outubro()

    da_b = _chamar(outubro.conectar(empresa_id=outubro.empresa_b), "comparar_meses", {})

    assert [i["competencia"] for i in da_b.structured_content["itens"]] == ["2026-09"]
    assert da_b.structured_content["itens"][0]["divergencias"]["divergente_valor"] == 4


@pytest.mark.parametrize(
    ("argumentos", "trecho"),
    [
        ({"quantidade": 0}, "vai de 1 a 12"),
        ({"quantidade": 13}, "vai de 1 a 12"),
        ({"ate": "2026-13"}, "Mês `ate` inválido"),
        ({"ate": "outubro"}, "Mês `ate` inválido"),
        ({"quantidade": "seis"}, "quantidade"),
    ],
)
@pytest.mark.usefixtures("mcp")
def test_comparar_meses_entrada_invalida_e_erro_da_ferramenta(cenario, argumentos, trecho):
    resultado = _chamar(cenario.conectar(), "comparar_meses", argumentos)

    assert resultado.is_error is True
    assert trecho in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_comparar_meses_sem_conciliacao_orienta(cenario, criar_empresa):
    vazia = criar_empresa(razao_social="Empresa Vazia Ltda", conta_id=cenario.admin.conta_id).id
    token = cenario.conectar(empresa_id=vazia)

    sem_nada = _chamar(token, "comparar_meses", {})
    antes_de_tudo = _chamar(cenario.conectar(), "comparar_meses", {"ate": "2026-01"})

    for resultado in (sem_nada, antes_de_tudo):
        assert resultado.is_error is True
        assert "ainda não tem conciliação" in _texto(resultado)


# --- grupos do mês ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_grupos_com_um_lancamento_do_banco_contra_varios_do_sistema(cenario, mes, monkeypatch):
    _lote(mes, monkeypatch, "2026-11", "banco")

    resultado = _chamar(cenario.conectar(), "grupos_do_mes", {"competencia": "2026-11"})

    grupos = resultado.structured_content
    assert (grupos["total"], grupos["truncado"], len(grupos["itens"])) == (1, False, 1)
    grupo = grupos["itens"][0]
    uuid.UUID(grupo["grupo_id"])
    assert grupo["lado_unico"] == "banco" and grupo["soma"] == "150000.00"
    assert grupo["unico"]["valor"] == "-150000.00" and grupo["unico"]["data"] == "2026-11-20"
    assert grupo["quantidade_do_outro_lado"] == 10 and grupo["truncado"] is False
    # Mesma data: do valor menor (mais negativo) para o maior.
    assert [m["valor"] for m in grupo["do_outro_lado"]] == [
        f"-{v}.00" for v in sorted(PARCELAS, reverse=True)
    ]
    assert [m["data"] for m in grupo["do_outro_lado"]] == sorted(
        m["data"] for m in grupo["do_outro_lado"]
    )
    # A soma que o grupo diz que fechou é a das parcelas, ao centavo.
    assert sum(abs(Decimal(m["valor"])) for m in grupo["do_outro_lado"]) == Decimal(grupo["soma"])
    assert _texto(resultado) == "Novembro de 2026: 1 agrupamento; mostrando 1 a partir do 1º."


@pytest.mark.usefixtures("mcp")
def test_grupos_com_um_lancamento_do_sistema_contra_varios_do_banco(cenario, mes, monkeypatch):
    _lote(mes, monkeypatch, "2026-12", "sistema")

    grupo = _chamar(
        cenario.conectar(), "grupos_do_mes", {"competencia": "2026-12"}
    ).structured_content["itens"][0]

    assert grupo["lado_unico"] == "sistema" and grupo["soma"] == "150000.00"
    assert grupo["unico"]["valor"] == "-150000.00" and grupo["quantidade_do_outro_lado"] == 10


@pytest.mark.usefixtures("mcp")
def test_grupos_batem_com_a_visao_e_com_o_resumo_do_mes(cenario, mes, monkeypatch):
    _lote(mes, monkeypatch, "2026-11", "banco")
    token = cenario.conectar()

    async def _funcao(cliente):
        return (
            await cliente.call_tool("grupos_do_mes", {"competencia": "2026-11"}),
            await cliente.call_tool("visao_do_mes", {"competencia": "2026-11"}),
            await cliente.call_tool("resumo_do_mes", {"competencia": "2026-11"}),
            await cliente.call_tool(
                "lancamentos_do_mes", {"competencia": "2026-11", "limite": 200}
            ),
        )

    grupos, visao, resumo, lancamentos = _com_cliente(token, _funcao)

    agrupamentos = visao.structured_content["agrupamentos"]
    assert agrupamentos == {"grupos": 1, "lancamentos": 11, "valor": "150000.00"}
    assert agrupamentos["grupos"] == grupos.structured_content["total"]
    assert resumo.structured_content["contagens"]["agrupamentos"] == 1
    # Os lançamentos do grupo são "conciliado" em lancamentos_do_mes, e nenhum vira divergência.
    assert {i["situacao"] for i in lancamentos.structured_content["itens"]} == {"conciliado"}
    ids_do_grupo = {m["id"] for m in grupos.structured_content["itens"][0]["do_outro_lado"]} | {
        grupos.structured_content["itens"][0]["unico"]["id"]
    }
    assert ids_do_grupo == {i["id"] for i in lancamentos.structured_content["itens"]}


@pytest.mark.usefixtures("mcp")
def test_grupos_competencia_omitida_e_mes_sem_grupos(cenario, mes, monkeypatch):
    _lote(mes, monkeypatch, "2026-11", "banco")
    token = cenario.conectar()

    omitida = _chamar(token, "grupos_do_mes", {})
    setembro = _chamar(token, "grupos_do_mes", {"competencia": "2026-09"})
    sem_conciliacao = _chamar(token, "grupos_do_mes", {"competencia": "2026-02"})

    assert omitida.structured_content["competencia"] == "2026-11"
    assert omitida.structured_content["total"] == 1
    assert setembro.structured_content["total"] == 0 and setembro.structured_content["itens"] == []
    assert _texto(setembro) == "Setembro de 2026: 0 agrupamentos."
    assert sem_conciliacao.structured_content["total"] == 0


@pytest.mark.usefixtures("mcp")
def test_grupos_nao_vazam_para_a_outra_empresa(cenario, mes, monkeypatch):
    _lote(mes, monkeypatch, "2026-11", "banco")

    da_b = _chamar(cenario.conectar(empresa_id=cenario.empresa_b), "grupos_do_mes", {})

    assert da_b.structured_content["total"] == 0
    assert "150000" not in json.dumps(da_b.structured_content)


@pytest.mark.parametrize(
    ("argumentos", "trecho"),
    [
        ({"competencia": "2026-13"}, "Competência inválida"),
        ({"limite": 0}, "limite vai de 1 a 25"),
        ({"limite": 26}, "limite vai de 1 a 25"),
        ({"deslocamento": -1}, "deslocamento"),
    ],
)
@pytest.mark.usefixtures("mcp")
def test_grupos_entrada_invalida_e_erro_da_ferramenta(cenario, argumentos, trecho):
    resultado = _chamar(cenario.conectar(), "grupos_do_mes", argumentos)

    assert resultado.is_error is True
    assert trecho in _texto(resultado)


# --- prompts (M4) ----------------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_o_servidor_lista_e_entrega_os_prompts(cenario):
    token = cenario.conectar()

    async def _funcao(cliente):
        lista = (await cliente.list_prompts()).prompts
        resumo = await cliente.get_prompt("resumo_de_fechamento", {"competencia": "2026-10"})
        priorizar = await cliente.get_prompt("priorizar_divergencias", {})
        return lista, resumo, priorizar

    lista, resumo, priorizar = _com_cliente(token, _funcao)

    assert {p.name for p in lista} == {"resumo_de_fechamento", "priorizar_divergencias"}
    for prompt in lista:
        assert prompt.title and prompt.description
        assert [a.name for a in prompt.arguments] == ["competencia"]
        assert not prompt.arguments[0].required
    texto_resumo = resumo.messages[0].content.text
    assert resumo.messages[0].role == "user"
    assert "o mês 2026-10" in texto_resumo
    for ferramenta in ("pendencias_do_mes", "visao_do_mes", "fechamentos", "divergencias_do_mes"):
        assert ferramenta in texto_resumo
    assert "o mês mais recente com conciliação" in priorizar.messages[0].content.text


@pytest.mark.usefixtures("mcp")
def test_os_prompts_so_citam_ferramentas_que_existem(cenario):
    token = cenario.conectar()

    async def _funcao(cliente):
        ferramentas = {f.name for f in (await cliente.list_tools()).tools}
        textos = [
            (await cliente.get_prompt(p.name, {})).messages[0].content.text
            for p in (await cliente.list_prompts()).prompts
        ]
        return ferramentas, textos

    ferramentas, textos = _com_cliente(token, _funcao)

    import re

    for texto in textos:
        citadas = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", texto)) - {"ordenar_por", "ordem"}
        assert citadas <= ferramentas | {"sem_justificativa"}, citadas - ferramentas


# --- privacidade, nas ferramentas do PR 2 ----------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_ferramentas_do_pr_2_nao_devolvem_dado_proibido(outubro, mes, monkeypatch):
    outubro.outubro()
    _lote(mes, monkeypatch, "2026-11", "banco")
    token = outubro.conectar()

    async def _funcao(cliente):
        saidas, nomes = [], set()

        async def chamar(nome, argumentos):
            nomes.add(nome)
            saidas.append(await cliente.call_tool(nome, argumentos))

        await chamar("comparar_meses", {"quantidade": 12})
        for competencia in ("2026-09", "2026-10", "2026-11"):
            await chamar("lancamentos_do_mes", {"competencia": competencia, "limite": 200})
            await chamar("grupos_do_mes", {"competencia": competencia})
        for prompt in (await cliente.list_prompts()).prompts:
            saidas.append(await cliente.get_prompt(prompt.name, {}))
        ferramentas = {f.name for f in (await cliente.list_tools()).tools}
        return saidas, nomes, ferramentas

    resultados, nomes, ferramentas = _com_cliente(token, _funcao)

    # Nenhuma ferramenta fica de fora da varredura de privacidade: as do PR 1 e as deste.
    assert nomes | FERRAMENTAS_VARRIDAS_NO_PR_1 == ferramentas
    for resultado in resultados:
        saida = json.dumps(
            [getattr(resultado, "structured_content", None), str(resultado)], ensure_ascii=False
        )
        for proibido in (
            DESCRICAO_SECRETA,
            DESCRICAO_TARIFA,
            "OUT banco match",
            "OUT sistema match",
            "NOV a",
            ARQUIVO_SECRETO,
            "extrato.csv",
            "parcela",
        ):
            assert proibido not in saida, proibido


# --- regra dos pares, para vários meses ------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_pares_dos_meses_e_o_mesmo_que_pares_do_mes_de_cada_um(outubro, mes):
    from app.core.database import SessionLocal
    from app.services import fechamentos

    outubro.outubro()
    _novembro(mes)
    competencias = ["2026-09", "2026-10", "2026-11", "2026-02"]

    with SessionLocal() as db:
        juntos = fechamentos.pares_dos_meses(db, outubro.empresa_a, competencias)
        separados = {c: fechamentos.pares_do_mes(db, outubro.empresa_a, c) for c in competencias}
        da_b = fechamentos.pares_dos_meses(db, outubro.empresa_b, competencias)

    assert set(juntos) == {"2026-09", "2026-10", "2026-11"}  # mês sem par fica fora
    for competencia in competencias:
        assert [
            (p.extrato_banco_id, p.extrato_sistema_id, p.rodada, p.execucao.id)
            for p in juntos.get(competencia, [])
        ] == [
            (p.extrato_banco_id, p.extrato_sistema_id, p.rodada, p.execucao.id)
            for p in separados[competencia]
        ]
    assert set(da_b) == {"2026-09"}
    assert fechamentos.pares_dos_meses(SessionLocal(), outubro.empresa_a, []) == {}
