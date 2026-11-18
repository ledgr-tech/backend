"""Ferramentas ampliadas do MCP, PR 1 da issue #225: visão do mês, detalhe da
divergência, divergências com filtros e ordenação, e pendências do mês.

Mesmo padrão de tests/test_mcp_servidor.py (cliente do SDK em processo, token do
fluxo OAuth, nenhum teste acessa a internet) e as mesmas fixtures (`cenario`: uma
conta, duas empresas, A e B, cada uma com setembro conciliado). Aqui A ganha
outubro de 2026, montado à mão com datas diferentes para cobrir `divergente_data`
com par gravado, tarifa e sem correspondência.
"""

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

import jsonschema
import pytest
from conftest import cabecalhos
from fastapi.testclient import TestClient
from test_mcp_oauth import mcp  # noqa: F401 - fixture usada com usefixtures
from test_mcp_servidor import (  # noqa: F401 - `cenario` é fixture
    BASE,
    DESCRICAO_SECRETA,
    FERRAMENTAS,
    NOME_DA_PESSOA,
    RESSALVA_SECRETA,
    _chamar,
    _com_cliente,
    _texto,
    cenario,
)

from app.core.config import settings
from app.models import Extrato, Lancamento
from main import app

# As ferramentas que `test_nenhuma_ferramenta_devolve_dado_proibido` percorre; as do PR 2 ficam em
# tests/test_mcp_lancamentos_comparacao_grupos.py, que confere que as duas listas juntas são todas.
FERRAMENTAS_VARRIDAS_NO_PR_1 = {
    "empresa_conectada",
    "meses_com_conciliacao",
    "resumo_do_mes",
    "visao_do_mes",
    "conciliacoes_do_mes",
    "divergencias_do_mes",
    "detalhe_da_divergencia",
    "pendencias_do_mes",
    "fechamentos",
}
ARQUIVO_SECRETO = "ARQUIVO-SECRETO-outubro.ofx"
JUSTIFICATIVA_DA_ANALISTA = "Justificativa secreta da analista de outubro"
NOME_DA_ANALISTA = "Analista Pessoa Secreta"
DESCRICAO_TARIFA = "TARIFA MANUTENCAO DE CONTA SEGREDO"

# (data, valor, descrição) de outubro de 2026 da empresa A. Esperado: 1 match exato,
# 1 divergente_data com as duas pontas (50,00 em 05/10 no banco e em 07/10 no
# sistema), 2 sem correspondência e 1 tarifa. Descrições todas diferentes, para a
# similaridade não aproximar nada.
BANCO_OUTUBRO = [
    (date(2026, 10, 1), "10.00", "OUT banco match"),
    (date(2026, 10, 5), "50.00", f"{DESCRICAO_SECRETA} cinquenta"),
    (date(2026, 10, 10), "-1500.00", f"{DESCRICAO_SECRETA} mil e quinhentos"),
    (date(2026, 10, 12), "300.00", f"{DESCRICAO_SECRETA} trezentos"),
    (date(2026, 10, 15), "-25.00", DESCRICAO_TARIFA),
]
SISTEMA_OUTUBRO = [
    (date(2026, 10, 1), "10.00", "OUT sistema match"),
    (date(2026, 10, 7), "50.00", f"{DESCRICAO_SECRETA} sistema cinquenta"),
]


def _lancamentos(db, extrato: Extrato, itens) -> None:
    for posicao, (data, valor, descricao) in enumerate(itens):
        db.add(
            Lancamento(
                empresa_id=extrato.empresa_id,
                extrato_id=extrato.id,
                data=data,
                valor=Decimal(valor),
                descricao=descricao,
                tipo="credito" if Decimal(valor) > 0 else "debito",
                hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                ocorrencia=1,
                posicao=posicao,
            )
        )
    db.commit()


@pytest.fixture
def outubro(cenario, db_session, criar_extrato_da_empresa):  # noqa: F811
    """Outubro de 2026 conciliado na empresa A, com saldo final igual nos dois lados."""

    def _semear(saldo_banco="1000.00", saldo_sistema="1000.00"):
        extratos = {}
        for origem, itens, saldo in (
            ("banco", BANCO_OUTUBRO, saldo_banco),
            ("sistema", SISTEMA_OUTUBRO, saldo_sistema),
        ):
            extrato = criar_extrato_da_empresa(cenario.empresa_a, origem)
            extrato.nome_arquivo = ARQUIVO_SECRETO
            extrato.periodo_inicio, extrato.periodo_fim = date(2026, 10, 1), date(2026, 10, 31)
            if saldo is not None:
                extrato.saldo_final, extrato.saldo_data = Decimal(saldo), date(2026, 10, 31)
            db_session.commit()
            _lancamentos(db_session, extrato, itens)
            extratos[origem] = extrato
        resposta = cenario.http.post(
            "/conciliacoes",
            headers=cabecalhos(cenario.jwt, cenario.empresa_a),
            json={
                "extrato_banco_id": str(extratos["banco"].id),
                "extrato_sistema_id": str(extratos["sistema"].id),
            },
        )
        assert resposta.status_code == 201, resposta.text
        return extratos

    cenario.outubro = _semear
    return cenario


def _divergencias(token: str, **argumentos) -> dict:
    resultado = _chamar(token, "divergencias_do_mes", argumentos)
    assert resultado.is_error is not True, _texto(resultado)
    return resultado.structured_content


def _item(divergencias: dict, valor: str) -> dict:
    return next(i for i in divergencias["itens"] if i["valor"] == valor)


def _decidir(contexto, extrato_banco_id, chave, jwt, tipo, texto=None):
    resposta = contexto.http.post(
        f"/conciliacoes/{extrato_banco_id}/decisoes",
        headers=cabecalhos(jwt, contexto.empresa_a),
        json={"chave": chave, "tipo": tipo, "texto": texto},
    )
    assert resposta.status_code == 200, resposta.text


# --- esquema e erros, por ferramenta ---------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_output_schema_presente_e_toda_resposta_adere_a_ele(outubro):
    outubro.outubro()
    token = outubro.conectar()

    async def _funcao(cliente):
        ferramentas = {f.name: f for f in (await cliente.list_tools()).tools}
        divergencias = await cliente.call_tool("divergencias_do_mes", {})
        chamadas = {
            "empresa_conectada": {},
            "meses_com_conciliacao": {},
            "resumo_do_mes": {},
            "visao_do_mes": {},
            "conciliacoes_do_mes": {},
            "divergencias_do_mes": {},
            "pendencias_do_mes": {},
            "lancamentos_do_mes": {},
            "comparar_meses": {},
            "grupos_do_mes": {},
            "fechamentos": {},
            "detalhe_da_divergencia": {"id": divergencias.structured_content["itens"][0]["id"]},
        }
        resultados = {n: await cliente.call_tool(n, a) for n, a in chamadas.items()}
        return ferramentas, resultados

    ferramentas, resultados = _com_cliente(token, _funcao)

    assert set(resultados) == FERRAMENTAS
    for nome, resultado in resultados.items():
        assert resultado.is_error is not True, (nome, _texto(resultado))
        esquema = ferramentas[nome].output_schema
        assert esquema is not None, nome
        jsonschema.validate(resultado.structured_content, esquema)


@pytest.mark.parametrize(
    ("nome", "argumentos", "trecho"),
    [
        ("visao_do_mes", {"competencia": "2026-13"}, "Competência inválida"),
        ("pendencias_do_mes", {"competencia": "outubro"}, "Competência inválida"),
        ("divergencias_do_mes", {"competencia": "26-10"}, "Competência inválida"),
        ("detalhe_da_divergencia", {"id": "isto-nao-e-um-uuid"}, "Divergência não encontrada"),
        ("detalhe_da_divergencia", {"id": str(uuid.uuid4())}, "Divergência não encontrada"),
        ("detalhe_da_divergencia", {}, "id"),
        ("divergencias_do_mes", {"valor_minimo": -1}, "não podem ser negativos"),
        ("divergencias_do_mes", {"valor_maximo": -1}, "não podem ser negativos"),
        (
            "divergencias_do_mes",
            {"valor_minimo": 10, "valor_maximo": 5},
            "mínimo não pode ser maior",
        ),
        ("divergencias_do_mes", {"valor_minimo": "mil"}, "valor_minimo"),
        ("divergencias_do_mes", {"ordenar_por": "nome"}, "Input should be 'data' or 'valor'"),
        ("divergencias_do_mes", {"ordem": "baixo"}, "Input should be 'asc' or 'desc'"),
        ("divergencias_do_mes", {"limite": 201}, "limite vai de 1 a 200"),
    ],
)
@pytest.mark.usefixtures("mcp")
def test_entrada_invalida_das_ferramentas_novas_e_erro_da_ferramenta(
    cenario, nome, argumentos, trecho  # noqa: F811
):
    resultado = _chamar(cenario.conectar(), nome, argumentos)

    assert resultado.is_error is True
    assert trecho in _texto(resultado)


@pytest.mark.parametrize("nome", sorted(FERRAMENTAS))
@pytest.mark.usefixtures("mcp")
def test_conta_inativa_barra_toda_ferramenta(cenario, db_session, nome):  # noqa: F811
    from sqlalchemy import func, update

    from app.core import conta_inativa
    from app.models import Conta

    token = cenario.conectar()
    db_session.execute(
        update(Conta)
        .where(Conta.id == cenario.admin.conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()
    corpo = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": nome, "arguments": {}},
    }

    with TestClient(app, base_url=BASE) as cliente:
        resposta = cliente.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=json.dumps(corpo),
        )

    assert (resposta.status_code, resposta.json()) == (403, conta_inativa.corpo())


# --- competência omitida e mês sem conciliação -----------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_competencia_omitida_e_o_mes_mais_recente_com_conciliacao(outubro):
    outubro.outubro()
    token = outubro.conectar()

    async def _funcao(cliente):
        return {
            nome: await cliente.call_tool(nome, {})
            for nome in (
                "resumo_do_mes",
                "visao_do_mes",
                "conciliacoes_do_mes",
                "divergencias_do_mes",
                "pendencias_do_mes",
            )
        }

    resultados = _com_cliente(token, _funcao)

    for nome, resultado in resultados.items():
        assert resultado.is_error is not True, (nome, _texto(resultado))
        assert "Outubro de 2026" in _texto(resultado), nome
    for nome in ("resumo_do_mes", "visao_do_mes", "divergencias_do_mes", "pendencias_do_mes"):
        assert resultados[nome].structured_content["competencia"] == "2026-10", nome


@pytest.mark.usefixtures("mcp")
def test_competencia_explicita_ganha_da_omitida(outubro):
    outubro.outubro()

    resultado = _chamar(outubro.conectar(), "visao_do_mes", {"competencia": "2026-09"})

    assert resultado.structured_content["competencia"] == "2026-09"
    assert "Setembro de 2026" in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_empresa_sem_nenhuma_conciliacao_orienta(cenario, criar_empresa):  # noqa: F811
    vazia = criar_empresa(razao_social="Empresa Vazia Ltda", conta_id=cenario.admin.conta_id).id
    token = cenario.conectar(empresa_id=vazia)

    async def _funcao(cliente):
        return {
            nome: await cliente.call_tool(nome, {})
            for nome in (
                "resumo_do_mes",
                "visao_do_mes",
                "conciliacoes_do_mes",
                "divergencias_do_mes",
                "pendencias_do_mes",
            )
        }

    for nome, resultado in _com_cliente(token, _funcao).items():
        assert resultado.is_error is True, nome
        assert "ainda não tem conciliação" in _texto(resultado), nome


@pytest.mark.parametrize("nome", ["visao_do_mes", "pendencias_do_mes"])
@pytest.mark.usefixtures("mcp")
def test_competencia_sem_conciliacao_orienta_a_usar_meses_com_conciliacao(
    cenario,  # noqa: F811
    nome,
):
    resultado = _chamar(cenario.conectar(), nome, {"competencia": "2026-07"})

    assert resultado.is_error is True
    assert "meses_com_conciliacao" in _texto(resultado)


# --- visão do mês ----------------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_visao_do_mes_tem_os_numeros_do_mes(outubro):
    outubro.outubro()

    resultado = _chamar(outubro.conectar(), "visao_do_mes", {"competencia": "2026-10"})

    visao = resultado.structured_content
    assert visao["fechado"] is False
    assert visao["banco"] == {
        "extratos": 1,
        "lancamentos": 5,
        "entradas": "360.00",
        "saidas": "-1525.00",
        "liquido": "-1165.00",
        "saldo_final": "1000.00",
        "saldo_data": "2026-10-31",
    }
    assert visao["sistema"] == {
        "extratos": 1,
        "lancamentos": 2,
        "entradas": "60.00",
        "saidas": "0.00",
        "liquido": "60.00",
        "saldo_final": "1000.00",
        "saldo_data": "2026-10-31",
    }
    assert visao["diferenca_liquida"] == "-1225.00"
    assert visao["saldos_batem"] is True and visao["diferenca_de_saldo"] == "0.00"
    # 1 linha conciliada de 5; no banco, R$ 10 conciliados de R$ 1.885 movimentados.
    assert visao["percentual_conciliado_quantidade"] == "20.00"
    assert visao["percentual_conciliado_valor"] == "0.53"
    assert visao["tarifa_bancaria"] == {"quantidade": 1, "valor": "25.00"}
    assert visao["duplicados"] == {"quantidade": 0, "valor": "0.00"}
    assert visao["agrupamentos"] == {"grupos": 0, "lancamentos": 0, "valor": "0.00"}
    assert _texto(resultado).startswith("Outubro de 2026: 20,0% das linhas conciliadas")
    assert "Os saldos finais do banco e do sistema batem." in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_visao_do_mes_com_saldos_diferentes_e_sem_saldo(outubro):
    outubro.outubro(saldo_banco="1000.00", saldo_sistema="990.00")
    resultado = _chamar(outubro.conectar(), "visao_do_mes", {"competencia": "2026-10"})
    visao = resultado.structured_content
    assert visao["saldos_batem"] is False and visao["diferenca_de_saldo"] == "10.00"
    assert "não batem: diferença de R$ 10,00" in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_visao_do_mes_sem_saldo_no_arquivo(cenario):  # noqa: F811
    resultado = _chamar(cenario.conectar(), "visao_do_mes", {"competencia": "2026-09"})

    visao = resultado.structured_content
    assert visao["saldos_batem"] is None and visao["diferenca_de_saldo"] is None
    assert visao["banco"]["saldo_final"] is None
    assert "indisponível em um dos lados" in _texto(resultado)
    assert visao["fechado"] is True


@pytest.mark.usefixtures("mcp")
def test_visao_do_mes_nunca_traz_dado_da_outra_empresa(outubro):
    outubro.outubro()

    da_b = _chamar(outubro.conectar(empresa_id=outubro.empresa_b), "visao_do_mes", {})

    assert da_b.structured_content["competencia"] == "2026-09"
    assert da_b.structured_content["banco"]["lancamentos"] == 4
    assert "1525.00" not in json.dumps(da_b.structured_content)


# --- divergências: filtros, ordenação e id ---------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_divergencias_sem_argumentos_novos_seguem_em_ordem_de_data(outubro):
    outubro.outubro()

    divergencias = _divergencias(outubro.conectar(), competencia="2026-10")

    assert divergencias["total"] == 4 and divergencias["truncado"] is False
    assert [i["data"] for i in divergencias["itens"]] == [
        "2026-10-05",
        "2026-10-10",
        "2026-10-12",
        "2026-10-15",
    ]
    assert {i["categoria"] for i in divergencias["itens"]} == {
        "divergente_data",
        "sem_correspondencia",
        "tarifa_bancaria",
    }
    ids = [i["id"] for i in divergencias["itens"]]
    assert len(set(ids)) == 4 and all(uuid.UUID(i) for i in ids)


@pytest.mark.usefixtures("mcp")
def test_as_dez_maiores_por_valor_absoluto(outubro):
    outubro.outubro()
    token = outubro.conectar()

    maiores = _divergencias(token, competencia="2026-10", ordenar_por="valor", ordem="desc")
    menores = _divergencias(token, competencia="2026-10", ordenar_por="valor", ordem="asc")
    duas = _divergencias(token, competencia="2026-10", ordenar_por="valor", ordem="desc", limite=2)

    # O débito de R$ 1.500 é a maior divergência: vale o valor absoluto.
    assert [i["valor"] for i in maiores["itens"]] == ["-1500.00", "300.00", "50.00", "-25.00"]
    assert [i["valor"] for i in menores["itens"]] == ["-25.00", "50.00", "300.00", "-1500.00"]
    assert [i["valor"] for i in duas["itens"]] == ["-1500.00", "300.00"]
    assert duas["total"] == 4 and duas["truncado"] is True


@pytest.mark.usefixtures("mcp")
def test_filtros_de_valor_minimo_e_maximo_valem_para_o_absoluto(outubro):
    outubro.outubro()
    token = outubro.conectar()

    acima_de_mil = _divergencias(token, competencia="2026-10", valor_minimo=1000)
    entre = _divergencias(token, competencia="2026-10", valor_minimo="50", valor_maximo=300)
    exato = _divergencias(token, competencia="2026-10", valor_minimo=300, valor_maximo=300)
    ate_cinquenta = _divergencias(
        token, competencia="2026-10", valor_maximo=50, ordenar_por="valor"
    )
    combinado = _divergencias(
        token, competencia="2026-10", categoria="sem_correspondencia", valor_minimo=1000
    )

    assert [i["valor"] for i in acima_de_mil["itens"]] == ["-1500.00"]
    assert sorted(i["valor"] for i in entre["itens"]) == ["300.00", "50.00"]
    assert [i["valor"] for i in exato["itens"]] == ["300.00"]
    assert [i["valor"] for i in ate_cinquenta["itens"]] == ["-25.00", "50.00"]
    assert combinado["total"] == 1
    assert _divergencias(token, competencia="2026-10", valor_minimo=2000)["itens"] == []


@pytest.mark.usefixtures("mcp")
def test_filtros_de_categoria_e_situacao_e_paginacao(outubro):
    outubro.outubro()
    token = outubro.conectar()

    tarifa = _divergencias(token, competencia="2026-10", categoria="tarifa_bancaria")
    sem = _divergencias(token, competencia="2026-10", situacao="sem_justificativa", limite=3)
    segunda = _resultado = _chamar(
        token,
        "divergencias_do_mes",
        {"competencia": "2026-10", "limite": 3, "deslocamento": 3},
    )

    assert [i["valor"] for i in tarifa["itens"]] == ["-25.00"]
    assert sem["total"] == 4 and len(sem["itens"]) == 3 and sem["truncado"] is True
    assert segunda.structured_content["truncado"] is False
    assert len(segunda.structured_content["itens"]) == 1
    assert "Há mais páginas" in _texto(_chamar(token, "divergencias_do_mes", {"limite": 3}))
    assert "Há mais páginas" not in _texto(segunda)


@pytest.mark.usefixtures("mcp")
def test_id_da_divergencia_de_a_nao_acha_nada_na_b(outubro):
    outubro.outubro()
    id_de_a = _divergencias(outubro.conectar(), competencia="2026-10")["itens"][0]["id"]
    token_b = outubro.conectar(empresa_id=outubro.empresa_b)

    de_a_na_b = _chamar(token_b, "detalhe_da_divergencia", {"id": id_de_a})
    inexistente = _chamar(token_b, "detalhe_da_divergencia", {"id": str(uuid.uuid4())})
    da_b = _divergencias(token_b, competencia="2026-09")

    assert de_a_na_b.is_error is True
    # Não revela que o id existe em outra empresa: a mesma mensagem do id que não existe.
    assert _texto(de_a_na_b) == _texto(inexistente)
    assert id_de_a not in json.dumps(da_b)


# --- detalhe da divergência ------------------------------------------------------------------


def _detalhe(token, divergencias, valor):
    resultado = _chamar(token, "detalhe_da_divergencia", {"id": _item(divergencias, valor)["id"]})
    assert resultado.is_error is not True, _texto(resultado)
    return resultado


@pytest.mark.usefixtures("mcp")
def test_detalhe_de_divergente_data_com_par_gravado(outubro):
    outubro.outubro()
    token = outubro.conectar()
    divergencias = _divergencias(token, competencia="2026-10")

    resultado = _detalhe(token, divergencias, "50.00")

    detalhe = resultado.structured_content
    assert detalhe["categoria"] == "divergente_data" and detalhe["competencia"] == "2026-10"
    assert detalhe["lancamento"] == {
        "lado": "banco",
        "data": "2026-10-05",
        "valor": "50.00",
        "tipo": "credito",
    }
    assert detalhe["contraparte"] == {
        "lado": "sistema",
        "data": "2026-10-07",
        "valor": "50.00",
        "tipo": "credito",
    }
    assert detalhe["contraparte_gravada"] is True
    assert detalhe["diferenca_de_valor"] == "0.00" and detalhe["diferenca_de_dias"] == 2
    assert detalhe["tolerancia_dias"] == 0
    assert detalhe["situacao"] == "sem_justificativa"
    rotulos = {d["rotulo"] for d in detalhe["detalhes"]}
    assert {"Banco", "Sistema", "Diferença de datas", "Tolerância da conciliação"} <= rotulos
    assert "2 dias" in detalhe["explicacao"] and "05/10/2026" in detalhe["explicacao"]
    assert "Divergência de outubro de 2026 (sem justificativa)." in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_detalhe_de_linha_so_de_um_lado_e_de_tarifa(outubro):
    outubro.outubro()
    token = outubro.conectar()
    divergencias = _divergencias(token, competencia="2026-10")

    grande = _detalhe(token, divergencias, "-1500.00").structured_content
    tarifa = _detalhe(token, divergencias, "-25.00").structured_content

    assert grande["categoria"] == "sem_correspondencia"
    assert grande["contraparte"] is None and grande["contraparte_gravada"] is None
    assert grande["diferenca_de_valor"] is None and grande["diferenca_de_dias"] is None
    assert grande["outros_candidatos"] == 0
    assert grande["lancamento"]["tipo"] == "debito"
    assert tarifa["categoria"] == "tarifa_bancaria"
    assert tarifa["contraparte"] is None


@pytest.mark.usefixtures("mcp")
def test_detalhe_mostra_a_situacao_da_decisao_sem_o_texto(
    monkeypatch, outubro, criar_usuario, gerar_token
):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    extratos = outubro.outubro()
    analista = criar_usuario(
        conta_id=outubro.admin.conta_id, papel="analista", nome=NOME_DA_ANALISTA
    )
    token = outubro.conectar()
    grande = _item(_divergencias(token, competencia="2026-10"), "-1500.00")

    _decidir(
        outubro,
        extratos["banco"].id,
        grande["chave"],
        gerar_token(analista.conta_id, usuario_id=analista.id),
        "justificada",
        JUSTIFICATIVA_DA_ANALISTA,
    )
    resultado = _chamar(token, "detalhe_da_divergencia", {"id": grande["id"]})

    assert resultado.structured_content["situacao"] == "aguardando_aprovacao"
    saida = json.dumps([resultado.structured_content, _texto(resultado)], ensure_ascii=False)
    assert JUSTIFICATIVA_DA_ANALISTA not in saida and NOME_DA_ANALISTA not in saida


# --- pendências do mês -----------------------------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_pendencias_de_mes_aberto_com_divergencias_sem_justificativa(outubro):
    outubro.outubro()
    token = outubro.conectar()

    resultado = _chamar(token, "pendencias_do_mes", {"competencia": "2026-10"})

    pendencias = resultado.structured_content
    assert pendencias["fechado"] is False
    assert pendencias["pode_fechar"] is False and pendencias["pode_fechar_com_ressalva"] is True
    assert [(m["codigo"], m["quantidade"], m["impede"]) for m in pendencias["motivos"]] == [
        ("divergencias_sem_justificativa", 4, "sem_ressalva")
    ]
    assert pendencias["sem_justificativa"]["total"] == 4
    assert pendencias["sem_justificativa"]["truncado"] is False
    assert pendencias["aguardando_aprovacao"] == {"total": 0, "ids": [], "truncado": False}
    # Os ids são os de divergencias_do_mes, das de maior valor para as de menor.
    divergencias = _divergencias(token, competencia="2026-10", ordenar_por="valor", ordem="desc")
    assert pendencias["sem_justificativa"]["ids"] == [i["id"] for i in divergencias["itens"]]
    assert _texto(resultado) == (
        "Outubro de 2026: não fecha sem ressalva (4 divergências sem justificativa); "
        "fecha com ressalva."
    )


@pytest.mark.usefixtures("mcp")
def test_pendencias_batem_com_o_resumo_do_mes(outubro):
    outubro.outubro()
    token = outubro.conectar()

    async def _funcao(cliente):
        return (
            await cliente.call_tool("resumo_do_mes", {"competencia": "2026-10"}),
            await cliente.call_tool("pendencias_do_mes", {"competencia": "2026-10"}),
        )

    resumo, pendencias = _com_cliente(token, _funcao)

    contagens = resumo.structured_content["contagens"]
    assert pendencias.structured_content["sem_justificativa"]["total"] == contagens["pendentes"]
    assert (
        pendencias.structured_content["aguardando_aprovacao"]["total"]
        == contagens["aguardando_aprovacao"]
    )
    assert (
        pendencias.structured_content["pode_fechar"]
        == resumo.structured_content["pronto_para_fechar"]
    )


@pytest.mark.usefixtures("mcp")
def test_justificativa_nao_aprovada_impede_fechar_ate_com_ressalva(
    monkeypatch, outubro, criar_usuario, gerar_token
):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    extratos = outubro.outubro()
    analista = criar_usuario(conta_id=outubro.admin.conta_id, papel="analista", nome="Outra Pessoa")
    token = outubro.conectar()
    divergencias = _divergencias(token, competencia="2026-10")
    jwt_analista = gerar_token(analista.conta_id, usuario_id=analista.id)

    _decidir(
        outubro,
        extratos["banco"].id,
        _item(divergencias, "300.00")["chave"],
        jwt_analista,
        "justificada",
        JUSTIFICATIVA_DA_ANALISTA,
    )
    resultado = _chamar(token, "pendencias_do_mes", {"competencia": "2026-10"})

    pendencias = resultado.structured_content
    assert pendencias["pode_fechar"] is False and pendencias["pode_fechar_com_ressalva"] is False
    assert {m["codigo"]: m["impede"] for m in pendencias["motivos"]} == {
        "justificativas_aguardando_aprovacao": "sempre",
        "divergencias_sem_justificativa": "sem_ressalva",
    }
    assert pendencias["aguardando_aprovacao"]["ids"] == [_item(divergencias, "300.00")["id"]]
    assert pendencias["sem_justificativa"]["total"] == 3
    assert "não pode ser fechado" in _texto(resultado)
    assert JUSTIFICATIVA_DA_ANALISTA not in json.dumps(pendencias)


@pytest.mark.usefixtures("mcp")
def test_pode_fechar_quando_tudo_esta_justificado_e_aprovado(monkeypatch, outubro):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    extratos = outubro.outubro()
    token = outubro.conectar()
    divergencias = _divergencias(token, competencia="2026-10")

    for item in divergencias["itens"]:
        _decidir(
            outubro, extratos["banco"].id, item["chave"], outubro.jwt, "justificada", "Conferido"
        )
        # Admin aprova a própria justificativa quando ela ainda espera aprovação.
        pendencia = _chamar(token, "pendencias_do_mes", {"competencia": "2026-10"})
        for aguardando in pendencia.structured_content["aguardando_aprovacao"]["ids"]:
            alvo = next(i for i in divergencias["itens"] if i["id"] == aguardando)
            _decidir(
                outubro, extratos["banco"].id, alvo["chave"], outubro.jwt, "justificativa_aprovada"
            )
    resultado = _chamar(token, "pendencias_do_mes", {"competencia": "2026-10"})

    pendencias = resultado.structured_content
    assert pendencias["pode_fechar"] is True and pendencias["motivos"] == []
    assert pendencias["sem_justificativa"]["total"] == 0
    assert _texto(resultado) == "Outubro de 2026: pode ser fechado, sem pendências."


@pytest.mark.usefixtures("mcp")
def test_pendencias_de_mes_fechado_nao_deixa_fechar(cenario):  # noqa: F811
    resultado = _chamar(cenario.conectar(), "pendencias_do_mes", {"competencia": "2026-09"})

    pendencias = resultado.structured_content
    assert pendencias["fechado"] is True
    assert pendencias["pode_fechar"] is False and pendencias["pode_fechar_com_ressalva"] is False
    assert pendencias["motivos"][0] == {
        "codigo": "mes_fechado",
        "descricao": "O mês já está fechado. Reabra-o no Ledgr para alterar.",
        "quantidade": 0,
        "impede": "sempre",
    }
    assert _texto(resultado) == "Setembro de 2026: já está fechado."


# --- privacidade, em todas as ferramentas ----------------------------------------------------


@pytest.mark.usefixtures("mcp")
def test_nenhuma_ferramenta_devolve_dado_proibido(monkeypatch, outubro, criar_usuario, gerar_token):
    """Descrição, justificativa, ressalva, nome de pessoa e nome de arquivo, todos com
    marcador, e nenhum aparece nas respostas de nenhuma ferramenta, nem no detalhe de
    cada uma das divergências dos dois meses."""
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    extratos = outubro.outubro()
    analista = criar_usuario(
        conta_id=outubro.admin.conta_id, papel="analista", nome=NOME_DA_ANALISTA
    )
    token = outubro.conectar()
    divergencias = _divergencias(token, competencia="2026-10")
    _decidir(
        outubro,
        extratos["banco"].id,
        _item(divergencias, "300.00")["chave"],
        gerar_token(analista.conta_id, usuario_id=analista.id),
        "justificada",
        JUSTIFICATIVA_DA_ANALISTA,
    )

    async def _funcao(cliente):
        saidas, nomes = [], set()

        async def chamar(nome, argumentos):
            nomes.add(nome)
            saidas.append(await cliente.call_tool(nome, argumentos))
            return saidas[-1]

        for competencia in ("2026-09", "2026-10"):
            lista = await chamar("divergencias_do_mes", {"competencia": competencia, "limite": 200})
            for item in lista.structured_content["itens"]:
                await chamar("detalhe_da_divergencia", {"id": item["id"]})
            for nome in (
                "resumo_do_mes",
                "visao_do_mes",
                "conciliacoes_do_mes",
                "pendencias_do_mes",
            ):
                await chamar(nome, {"competencia": competencia})
        for nome in ("empresa_conectada", "meses_com_conciliacao", "fechamentos"):
            await chamar(nome, {})
        return saidas, nomes

    resultados, nomes = _com_cliente(token, _funcao)

    assert nomes == FERRAMENTAS_VARRIDAS_NO_PR_1
    assert len(resultados) > 20
    for resultado in resultados:
        assert resultado.is_error is not True, _texto(resultado)
        saida = json.dumps([resultado.structured_content, _texto(resultado)], ensure_ascii=False)
        for proibido in (
            DESCRICAO_SECRETA,
            DESCRICAO_TARIFA,
            "OUT banco match",
            "OUT sistema match",
            JUSTIFICATIVA_DA_ANALISTA,
            "Texto confidencial da justificativa",
            RESSALVA_SECRETA,
            NOME_DA_PESSOA,
            NOME_DA_ANALISTA,
            ARQUIVO_SECRETO,
            "extrato.csv",
        ):
            assert proibido not in saida, proibido
