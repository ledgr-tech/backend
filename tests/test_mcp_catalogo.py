# ruff: noqa: F811
"""Catálogo de 17 perguntas da issue #225: cada uma tem caminho de resposta com as
ferramentas, e o catálogo inteiro cabe no limite de uso da conexão.

A tabela "pergunta, ferramenta, argumentos" do ledgr-docs
(07-tecnico/mcp-catalogo-de-perguntas) é a leitura humana deste arquivo: aqui cada
pergunta vira a sequência de chamadas de um assistente, numa só conexão, com os
fatos que a resposta precisa ter conferidos. As requisições HTTP são contadas de
verdade (o limite de `LIMITE_MCP_POR_CONEXAO` vale por requisição, não por
chamada de ferramenta), no pior caso de protocolo: o cliente legado, que gasta
três requisições para abrir a sessão e listar as ferramentas.

Dados (empresa A): setembro (4 divergências de valor, fechado), outubro (a mão, com
tarifa, divergente_data e sem correspondência) e novembro, com o lote de R$ 150.000,00
conciliado por agrupamento, um lançamento de R$ 1.250,00 conciliado e um débito de
R$ 5.500,00 sem correspondência.
"""

from datetime import date

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_mcp_ferramentas_ampliadas import DESCRICAO_SECRETA, outubro  # noqa: F401
from test_mcp_lancamentos_comparacao_grupos import PARCELAS, mes  # noqa: F401
from test_mcp_oauth import mcp  # noqa: F401 - fixture usada com usefixtures
from test_mcp_servidor import BASE, cenario  # noqa: F401 - `cenario` é fixture

from app.core.config import settings
from app.core.rate_limit import LIMITE_MCP_POR_CONEXAO
from main import app

REQUISICOES_DA_SESSAO_LEGADA = 3  # initialize, notifications/initialized e tools/list
MAXIMO_DE_CHAMADAS_POR_PERGUNTA = 8


def _novembro(mes) -> None:
    banco = [
        (date(2026, 11, 20), "-150000.00", f"{DESCRICAO_SECRETA} lote"),
        (date(2026, 11, 3), "1250.00", f"{DESCRICAO_SECRETA} mil duzentos e cinquenta"),
        (date(2026, 11, 12), "-5500.00", f"{DESCRICAO_SECRETA} cinco mil e quinhentos"),
        (date(2026, 11, 12), "100.00", f"{DESCRICAO_SECRETA} cem"),
    ]
    sistema = [
        (date(2026, 11, 20), f"-{valor}.00", f"{DESCRICAO_SECRETA} parcela {i}")
        for i, valor in enumerate(PARCELAS)
    ] + [(date(2026, 11, 3), "1250.00", f"{DESCRICAO_SECRETA} mil duzentos e cinquenta sistema")]
    resposta = mes("2026-11", banco, sistema)
    assert resposta["match_agrupado"] == 11 and resposta["agrupamentos"] == 1


def _com_cliente_contando(token: str, funcao):
    """Como `_com_cliente`, no modo legado, e devolve também quantas requisições HTTP foram."""
    requisicoes = []

    async def _contar(requisicao):
        requisicoes.append(requisicao.url.path)

    async def _rodar():
        async with app.router.lifespan_context(app), httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"request": [_contar]},
        ) as http:
            transporte = streamable_http_client(f"{BASE}/mcp", http_client=http)
            async with Client(transporte, mode="legacy") as cliente:
                await cliente.list_tools()
                return await funcao(cliente)

    resultado = anyio.run(_rodar)
    return resultado, len(requisicoes)


async def _perguntas(cliente) -> tuple[dict[int, list[str]], dict[int, dict]]:
    """As 17 perguntas, cada uma como o assistente a responderia. Devolve as ferramentas
    chamadas por pergunta e o que a resposta de cada uma precisa dizer."""
    chamadas: dict[int, list[str]] = {}
    fatos: dict[int, dict] = {}

    async def usar(pergunta: int, nome: str, **argumentos) -> dict:
        resultado = await cliente.call_tool(nome, argumentos)
        assert resultado.is_error is not True, (pergunta, nome, resultado.content)
        chamadas.setdefault(pergunta, []).append(nome)
        return resultado.structured_content

    # Visão do mês
    resumo = await usar(1, "resumo_do_mes", competencia="2026-11")
    visao = await usar(1, "visao_do_mes", competencia="2026-11")
    fatos[1] = {
        "fechado": resumo["fechado"],
        "percentual": visao["percentual_conciliado_quantidade"],
    }
    visao = await usar(2, "visao_do_mes", competencia="2026-11")
    fatos[2] = {
        "banco": visao["banco"]["liquido"],
        "sistema": visao["sistema"]["liquido"],
        "diferenca": visao["diferenca_liquida"],
    }
    visao = await usar(3, "visao_do_mes", competencia="2026-11")
    fatos[3] = {"saldos_batem": visao["saldos_batem"]}
    comparacao = await usar(4, "comparar_meses", quantidade=2, ate="2026-11")
    await usar(4, "visao_do_mes", competencia="2026-10")
    await usar(4, "visao_do_mes", competencia="2026-11")
    fatos[4] = {"meses": [m["competencia"] for m in comparacao["itens"]]}
    comparacao = await usar(5, "comparar_meses", quantidade=6)
    fatos[5] = {"meses": [m["competencia"] for m in comparacao["itens"]]}

    # Divergências
    maiores = await usar(
        6,
        "divergencias_do_mes",
        competencia="2026-11",
        ordenar_por="valor",
        ordem="desc",
        limite=10,
    )
    fatos[6] = {"valores": [i["valor"] for i in maiores["itens"]]}
    acima = await usar(
        7,
        "divergencias_do_mes",
        competencia="2026-11",
        categoria="sem_correspondencia",
        valor_minimo=1000,
    )
    fatos[7] = {"valores": [i["valor"] for i in acima["itens"]]}
    pendencias = await usar(8, "pendencias_do_mes", competencia="2026-11")
    sem = await usar(8, "divergencias_do_mes", competencia="2026-11", situacao="sem_justificativa")
    fatos[8] = {
        "sem_justificativa": pendencias["sem_justificativa"]["total"],
        "aguardando": pendencias["aguardando_aprovacao"]["total"],
        "listadas": sem["total"],
    }
    achada = await usar(
        9, "divergencias_do_mes", competencia="2026-11", valor_minimo=100, valor_maximo=100
    )
    detalhe = await usar(9, "detalhe_da_divergencia", id=achada["itens"][0]["id"])
    fatos[9] = {"categoria": detalhe["categoria"], "tem_explicacao": bool(detalhe["explicacao"])}
    visao = await usar(10, "visao_do_mes", competencia="2026-10")
    tarifas = await usar(
        10, "divergencias_do_mes", competencia="2026-10", categoria="tarifa_bancaria"
    )
    fatos[10] = {
        "tarifa": visao["tarifa_bancaria"],
        "duplicados": visao["duplicados"],
        "listadas": tarifas["total"],
    }
    grupos = await usar(11, "grupos_do_mes", competencia="2026-11")
    fatos[11] = {
        "grupos": grupos["total"],
        "soma": grupos["itens"][0]["soma"],
        "do_outro_lado": grupos["itens"][0]["quantidade_do_outro_lado"],
    }

    # Lançamentos
    mil = await usar(
        12, "lancamentos_do_mes", competencia="2026-11", valor_minimo=1250, valor_maximo=1250
    )
    fatos[12] = {"situacoes": sorted(i["situacao"] for i in mil["itens"])}
    dia = await usar(
        13,
        "lancamentos_do_mes",
        competencia="2026-11",
        lado="banco",
        data="2026-11-12",
        situacao="sem_correspondencia",
    )
    fatos[13] = {"valores": sorted(i["valor"] for i in dia["itens"])}
    debitos = await usar(
        14,
        "lancamentos_do_mes",
        competencia="2026-11",
        tipo="debito",
        valor_minimo=5000,
        ordenar_por="valor",
        ordem="desc",
    )
    fatos[14] = {"total": debitos["total"], "valores": [i["valor"] for i in debitos["itens"]]}

    # Fechamento
    pendencias = await usar(15, "pendencias_do_mes", competencia="2026-11")
    fatos[15] = {
        "pode_fechar": pendencias["pode_fechar"],
        "motivos": [m["codigo"] for m in pendencias["motivos"]],
    }
    meses = await usar(16, "meses_com_conciliacao")
    setembro = await usar(16, "fechamentos", competencia="2026-09")
    fatos[16] = {
        "abertos": [m["competencia"] for m in meses["itens"] if not m["fechado"]],
        "setembro_fechado_em": setembro["itens"][0]["fechado_em"] is not None,
    }

    # Resumo para o cliente
    empresa = await usar(17, "empresa_conectada")
    await usar(17, "resumo_do_mes", competencia="2026-11")
    await usar(17, "visao_do_mes", competencia="2026-11")
    await usar(17, "pendencias_do_mes", competencia="2026-11")
    await usar(
        17,
        "divergencias_do_mes",
        competencia="2026-11",
        ordenar_por="valor",
        ordem="desc",
        limite=10,
    )
    await usar(17, "fechamentos", competencia="2026-11")
    await usar(17, "comparar_meses")
    fatos[17] = {"empresa": empresa["razao_social"]}
    return chamadas, fatos


@pytest.mark.usefixtures("mcp")
def test_as_17_perguntas_do_catalogo_tem_caminho_e_cabem_no_limite(outubro, mes, monkeypatch):
    monkeypatch.setattr(settings, "agrupamento_habilitado", True)
    outubro.outubro()
    _novembro(mes)

    (chamadas, fatos), requisicoes = _com_cliente_contando(outubro.conectar(), _perguntas)

    assert sorted(chamadas) == list(range(1, 18))
    # Pergunta 1 a 3: o mês em dinheiro e em contagens.
    assert fatos[1]["fechado"] is False and fatos[1]["percentual"] is not None
    assert fatos[2] == {"banco": "-154150.00", "sistema": "-148750.00", "diferenca": "-5400.00"}
    assert fatos[3] == {"saldos_batem": None}  # o arquivo do teste não traz saldo
    # 4 e 5: comparação.
    assert fatos[4]["meses"] == ["2026-10", "2026-11"]
    assert fatos[5]["meses"] == ["2026-09", "2026-10", "2026-11"]
    # 6 e 7: as maiores e as acima de R$ 1.000. O lote do banco é conciliado, não é divergência.
    assert fatos[6]["valores"] == ["-5500.00", "100.00"]
    assert fatos[7]["valores"] == ["-5500.00"]
    # 8 e 9: o que está sem justificativa e a explicação de uma divergência.
    assert fatos[8] == {"sem_justificativa": 2, "aguardando": 0, "listadas": 2}
    assert fatos[9] == {"categoria": "sem_correspondencia", "tem_explicacao": True}
    # 10 e 11: tarifa, duplicados e agrupamento.
    assert fatos[10] == {
        "tarifa": {"quantidade": 1, "valor": "25.00"},
        "duplicados": {"quantidade": 0, "valor": "0.00"},
        "listadas": 1,
    }
    assert fatos[11] == {"grupos": 1, "soma": "150000.00", "do_outro_lado": 10}
    # 12 a 14: lançamentos.
    assert fatos[12] == {"situacoes": ["conciliado", "conciliado"]}
    assert fatos[13] == {"valores": ["-5500.00", "100.00"]}
    # O lote e as dez parcelas (todas de R$ 5.000,00 ou mais) entram: é lançamento, conciliado ou não.
    assert fatos[14]["total"] == 12 and fatos[14]["valores"][0] == "-150000.00"
    assert "-5500.00" in fatos[14]["valores"]
    # 15 e 16: fechamento.
    assert fatos[15] == {"pode_fechar": False, "motivos": ["divergencias_sem_justificativa"]}
    assert fatos[16] == {
        "abertos": ["2026-11", "2026-10"],
        "setembro_fechado_em": True,
    }
    assert fatos[17]["empresa"]

    por_pergunta = {n: len(c) for n, c in chamadas.items()}
    total = sum(por_pergunta.values())
    limite = int(LIMITE_MCP_POR_CONEXAO.split("/")[0])
    print(
        f"\nCATALOGO chamadas por pergunta={por_pergunta} total={total} "
        f"requisicoes_http={requisicoes} (limite {LIMITE_MCP_POR_CONEXAO})"
    )
    assert max(por_pergunta.values()) <= MAXIMO_DE_CHAMADAS_POR_PERGUNTA
    # Uma requisição por chamada de ferramenta, mais a abertura da sessão; as 17 perguntas
    # numa conexão só, dentro de um minuto, sem 429.
    assert requisicoes == total + REQUISICOES_DA_SESSAO_LEGADA
    assert requisicoes <= limite
