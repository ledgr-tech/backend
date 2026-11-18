"""As consultas do assistente com dados (issue #223, ADR-028).

O modelo só escolhe a consulta; quem busca no banco e escreve a resposta é o
backend, com textos prontos. Nenhuma resposta traz texto livre do banco
(descrição, justificativa, ressalva, nome de arquivo ou de pessoa), porque
ela volta no histórico da próxima pergunta.
"""

import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services import api_publica
from app.services.ajuda.consultas import (
    JANELA_MESES,
    Recusa,
    executar_somente_leitura,
    resolver_competencia,
    responder,
)
from app.services.ia.base import ConsultaDeDados
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
OUTUBRO = (date(2026, 10, 1), date(2026, 10, 31))
DESCRICAO_SECRETA = "PIX RECEBIDO MARIA DA SILVA"
JUSTIFICATIVA_SECRETA = "Acordo verbal com o fornecedor"
RESSALVA_SECRETA = "Faltou o extrato da poupança"
ARQUIVO_SECRETO = "extrato_itau_conta_98765.ofx"
MES_ATUAL = "2026-11"


def _consulta(intencao, competencia=None, meses_atras=None, categoria=None):
    return ConsultaDeDados(
        intencao=intencao, competencia=competencia, meses_atras=meses_atras, categoria=categoria
    )


@pytest.fixture
def empresa(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """Uma conta com a empresa dela e fábricas de par conciliado pela API do app."""
    admin = criar_usuario()
    empresa_id = empresa_unica(db_session, admin.conta_id)
    headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id), empresa_id)

    def extrato(origem, itens, periodo, saldo=None):
        novo = criar_extrato_da_empresa(empresa_id, origem)
        inserir_lancamentos(novo, list(itens))
        novo.periodo_inicio, novo.periodo_fim = periodo
        novo.nome_arquivo = ARQUIVO_SECRETO
        if saldo is not None:
            novo.saldo_final, novo.saldo_data = saldo
        db_session.commit()
        return novo

    def par(itens_banco, itens_sistema, periodo=SETEMBRO, saldo=None):
        banco = extrato("banco", itens_banco, periodo, saldo)
        sistema = extrato("sistema", itens_sistema, periodo)
        resposta = client.post(
            "/conciliacoes",
            headers=headers,
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )
        assert resposta.status_code == 201, resposta.text
        return banco

    def itens(banco):
        return client.get(f"/conciliacoes/{banco.id}", headers=headers).json()["itens"]

    def decidir(banco, valor, tipo, texto=None):
        chave = next(
            item["chave"]
            for item in itens(banco)
            if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == valor
        )
        resposta = client.post(
            f"/conciliacoes/{banco.id}/decisoes",
            headers=headers,
            json={"chave": chave, "tipo": tipo, "texto": texto},
        )
        assert resposta.status_code == 200, resposta.text

    def fechar(competencia="2026-09", ressalva=None):
        corpo = {"competencia": competencia}
        if ressalva is not None:
            corpo["ressalva"] = ressalva
        resposta = client.post("/fechamentos", headers=headers, json=corpo)
        assert resposta.status_code == 201, resposta.text

    def perguntar(consulta):
        return responder(db_session, empresa_id, consulta, MES_ATUAL)

    return SimpleNamespace(
        id=empresa_id,
        conta_id=admin.conta_id,
        par=par,
        itens=itens,
        decidir=decidir,
        fechar=fechar,
        perguntar=perguntar,
    )


def _setembro_com_divergencias(empresa, saldo=None):
    """Um match, uma diferença de valor (20 contra 7), uma tarifa e um
    lançamento só do banco."""
    return empresa.par(
        [
            ("10.00", "a"),
            ("20.00", DESCRICAO_SECRETA),
            ("-5.50", "TARIFA PACOTE SERVICOS"),
            ("33.30", "d"),
        ],
        [("10.00", "a"), ("7.00", DESCRICAO_SECRETA)],
        saldo=saldo,
    )


def _sem_texto_do_banco(resposta):
    tudo = resposta.texto + " ".join(link.titulo + link.caminho for link in resposta.links)
    for secreto in (DESCRICAO_SECRETA, JUSTIFICATIVA_SECRETA, RESSALVA_SECRETA, ARQUIVO_SECRETO):
        assert secreto not in tudo


# --- competência ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("competencia", "meses_atras", "esperado"),
    [
        ("2026-09", None, "2026-09"),
        ("2026-11", None, "2026-11"),
        (None, 0, "2026-11"),
        (None, 1, "2026-10"),
        (None, 11, "2025-12"),
        (None, None, None),
        ("2024-12", None, "2024-12"),
    ],
)
def test_resolver_competencia(competencia, meses_atras, esperado):
    assert resolver_competencia(competencia, meses_atras, MES_ATUAL) == esperado


@pytest.mark.parametrize(
    ("competencia", "meses_atras"),
    [
        ("2026-12", None),
        ("2024-11", None),
        (None, JANELA_MESES),
        (None, -1),
        ("2026-13", None),
        ("setembro", None),
    ],
)
def test_competencia_fora_da_janela_futura_ou_invalida_e_recusa(competencia, meses_atras):
    assert isinstance(resolver_competencia(competencia, meses_atras, MES_ATUAL), Recusa)


# --- meses ---------------------------------------------------------------------------


def test_meses_lista_os_meses_com_o_estado(empresa):
    _setembro_com_divergencias(empresa)
    empresa.par([("1.00", "x")], [("1.00", "x")], periodo=OUTUBRO)
    empresa.fechar("2026-09", ressalva=RESSALVA_SECRETA)

    resposta = empresa.perguntar(_consulta("meses"))

    assert resposta.texto == (
        "Meses com conciliação: outubro/2026 (aberto) e setembro/2026 (fechado)."
    )
    assert [link.caminho for link in resposta.links] == ["/fechamentos", "/historico"]
    _sem_texto_do_banco(resposta)


def test_meses_sem_conciliacao(empresa):
    resposta = empresa.perguntar(_consulta("meses"))

    assert resposta.texto == "Esta empresa ainda não tem conciliação."


# --- resumo do mês -------------------------------------------------------------------


def test_resumo_do_mes_tem_os_numeros_da_api_publica(empresa, db_session):
    banco = _setembro_com_divergencias(empresa)
    empresa.decidir(banco, "33.30", "justificada", JUSTIFICATIVA_SECRETA)
    c = api_publica.resumo_do_mes(db_session, empresa.id, "2026-09").contagens
    conciliadas = c.match_exato + c.match_tolerancia + c.match_agrupado

    resposta = empresa.perguntar(_consulta("resumo_do_mes", competencia="2026-09"))

    assert resposta.texto.startswith(
        f"Setembro/2026: {c.total} linhas, {conciliadas} conciliada"
        f"{'s' if conciliadas != 1 else ''} e {c.total - conciliadas} divergências"
    )
    assert f"{c.pendentes} sem justificativa" in resposta.texto
    assert "1 justificada" in resposta.texto
    assert resposta.texto.endswith("Mês aberto.")
    assert resposta.links[0].caminho == f"/conciliacoes/{banco.id}"
    assert resposta.links[0].titulo == "Conciliação de setembro/2026"
    _sem_texto_do_banco(resposta)


def test_resumo_do_mes_fechado_com_ressalva(empresa):
    _setembro_com_divergencias(empresa)
    empresa.fechar("2026-09", ressalva=RESSALVA_SECRETA)

    resposta = empresa.perguntar(_consulta("resumo_do_mes", competencia="2026-09"))

    assert resposta.texto.endswith("Mês fechado com ressalva.")
    _sem_texto_do_banco(resposta)


def test_resumo_sem_mes_usa_o_mais_recente(empresa):
    empresa.par([("1.00", "x")], [("1.00", "x")], periodo=OUTUBRO)

    resposta = empresa.perguntar(_consulta("resumo_do_mes"))

    assert resposta.texto == (
        "Outubro/2026: 1 linha, 1 conciliada e 0 divergências. Mês aberto e pronto para fechar."
    )


def test_mes_sem_conciliacao_aponta_o_mais_recente(empresa):
    empresa.par([("1.00", "x")], [("1.00", "x")], periodo=OUTUBRO)

    resposta = empresa.perguntar(_consulta("resumo_do_mes", meses_atras=0))

    assert resposta.texto == (
        "Novembro/2026 ainda não tem conciliação. A mais recente é de outubro/2026."
    )
    assert [link.caminho for link in resposta.links] == ["/historico"]


def test_mes_fora_da_janela_explica_o_limite(empresa):
    resposta = empresa.perguntar(_consulta("resumo_do_mes", competencia="2020-01"))

    assert resposta.texto == f"Eu consulto só os últimos {JANELA_MESES} meses."


def test_mes_futuro_nao_tem_conciliacao(empresa):
    resposta = empresa.perguntar(_consulta("saldo", competencia="2027-01"))

    assert resposta.texto == "Esse mês ainda não chegou."


# --- divergências ----------------------------------------------------------------------


def test_divergencias_do_mes_por_categoria(empresa, db_session):
    _setembro_com_divergencias(empresa)
    linhas = api_publica.divergencias_do_mes(db_session, empresa.id, "2026-09").itens
    tarifas = [d for d in linhas if d.categoria == "tarifa_bancaria"]

    resposta = empresa.perguntar(
        _consulta("divergencias_do_mes", competencia="2026-09", categoria="tarifa_bancaria")
    )

    assert len(tarifas) == 1
    assert resposta.texto == "Setembro/2026 tem 1 tarifa bancária. 1 está sem justificativa."
    _sem_texto_do_banco(resposta)


def test_divergencias_do_mes_todas_com_o_detalhe(empresa, db_session):
    _setembro_com_divergencias(empresa)
    linhas = api_publica.divergencias_do_mes(db_session, empresa.id, "2026-09").itens

    resposta = empresa.perguntar(_consulta("divergencias_do_mes", competencia="2026-09"))

    assert resposta.texto.startswith(f"Setembro/2026 tem {len(linhas)} divergências: ")
    assert "1 tarifa bancária" in resposta.texto
    assert resposta.texto.endswith(f"{len(linhas)} estão sem justificativa.")


def test_categoria_sem_divergencia(empresa):
    _setembro_com_divergencias(empresa)

    resposta = empresa.perguntar(
        _consulta("divergencias_do_mes", competencia="2026-09", categoria="duplicado")
    )

    assert resposta.texto == "Setembro/2026 não tem lançamento duplicado."


# --- em aberto -------------------------------------------------------------------------


def _em_aberto_pela_regra_do_front(itens):
    """`valorEmAberto` de app/(app)/e/[empresa]/dashboard/resumo.ts, no front:
    linhas não batidas e sem justificativa aprovada; com os dois lados, a
    diferença; com um, o valor."""
    total = Decimal(0)
    linhas = 0
    for item in itens:
        if item["status"] in ("match_exato", "match_tolerancia", "match_agrupado"):
            continue
        decisao = item["decisao"]
        if decisao and decisao["tipo"] == "justificada" and decisao["situacao"] == "aprovada":
            continue
        linhas += 1
        banco, sistema = item["lancamento_banco"], item["lancamento_sistema"]
        if banco and sistema:
            total += abs(Decimal(banco["valor"]) - Decimal(sistema["valor"]))
        else:
            total += abs(Decimal((banco or sistema)["valor"]))
    return total, linhas


def test_em_aberto_segue_a_regra_da_visao_geral(empresa):
    banco = _setembro_com_divergencias(empresa)
    empresa.decidir(banco, "33.30", "justificada", JUSTIFICATIVA_SECRETA)
    total, linhas = _em_aberto_pela_regra_do_front(empresa.itens(banco))

    resposta = empresa.perguntar(_consulta("em_aberto", competencia="2026-09"))

    reais = f"R$ {total:.2f}".replace(".", ",")
    assert resposta.texto == (
        f"Em setembro/2026 ficaram {reais} em aberto, em {linhas} linhas que ainda pedem decisão."
    )
    _sem_texto_do_banco(resposta)


def test_sem_valor_em_aberto(empresa):
    empresa.par([("1.00", "x")], [("1.00", "x")], periodo=OUTUBRO)

    resposta = empresa.perguntar(_consulta("em_aberto", competencia="2026-10"))

    assert resposta.texto == "Outubro/2026 não tem valor em aberto."


# --- saldo -----------------------------------------------------------------------------


def test_saldo_do_mes_com_inicial_e_final(empresa):
    _setembro_com_divergencias(empresa, saldo=(Decimal("1000.00"), date(2026, 9, 30)))

    resposta = empresa.perguntar(_consulta("saldo", competencia="2026-09"))

    # inicial = 1.000,00 - (10,00 + 20,00 - 5,50 + 33,30)
    assert resposta.texto == (
        "Extrato do banco de setembro/2026: saldo inicial de R$ 942,20 "
        "e saldo final de R$ 1.000,00 em 30/09/2026."
    )
    _sem_texto_do_banco(resposta)


def test_saldo_nao_informado_nunca_vira_zero(empresa):
    _setembro_com_divergencias(empresa)

    resposta = empresa.perguntar(_consulta("saldo", competencia="2026-09"))

    assert resposta.texto == "O arquivo do extrato do banco de setembro/2026 não informa o saldo."
    assert "0,00" not in resposta.texto


def test_saldo_com_dois_extratos_do_banco(empresa):
    _setembro_com_divergencias(empresa, saldo=(Decimal("1000.00"), date(2026, 9, 30)))
    empresa.par([("1.00", "x")], [("1.00", "x")])

    resposta = empresa.perguntar(_consulta("saldo", competencia="2026-09"))

    assert resposta.texto.startswith("Setembro/2026 tem 2 extratos do banco. ")
    assert "não informa o saldo" in resposta.texto
    assert "saldo final de R$ 1.000,00 em 30/09/2026" in resposta.texto
    assert len(resposta.links) == 2


# --- isolamento e só leitura -------------------------------------------------------------


def test_outra_empresa_da_mesma_conta_nao_entra(empresa, db_session, criar_empresa):
    _setembro_com_divergencias(empresa)
    outra = criar_empresa(razao_social="Outra Empresa Ltda", conta_id=empresa.conta_id)

    resposta = responder(db_session, outra.id, _consulta("meses"), MES_ATUAL)

    assert resposta.texto == "Esta empresa ainda não tem conciliação."


def test_consulta_roda_em_transacao_so_de_leitura(db_session, empresa):
    def tenta_gravar():
        db_session.execute(text("CREATE TEMP TABLE assistente_tentou_gravar (x int)"))

    with pytest.raises(DBAPIError) as erro:
        executar_somente_leitura(db_session, tenta_gravar)
    assert "read-only" in str(erro.value).lower()
    # A sessão continua usável depois da recusa.
    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_consulta_de_leitura_devolve_o_resultado(db_session, empresa):
    assert executar_somente_leitura(db_session, lambda: 42) == 42


def test_nenhum_numero_da_resposta_sai_do_banco_sem_formatacao(empresa):
    """Valores em reais sempre com vírgula e milhar com ponto (pt-BR)."""
    _setembro_com_divergencias(empresa, saldo=(Decimal("12345.67"), date(2026, 9, 30)))

    resposta = empresa.perguntar(_consulta("saldo", competencia="2026-09"))

    assert "R$ 12.345,67" in resposta.texto
    assert not re.search(r"\d\.\d{2}\b", resposta.texto)


def test_divergencias_contam_a_conferida_como_o_resumo_do_mes(empresa, db_session):
    """A conferida continua pendente (não tem justificativa), como no resumo do
    mês e na Visão geral: as duas respostas dizem o mesmo número."""
    banco = _setembro_com_divergencias(empresa)
    empresa.decidir(banco, "33.30", "conferida")
    pendentes = api_publica.resumo_do_mes(db_session, empresa.id, "2026-09").contagens.pendentes

    resposta = empresa.perguntar(_consulta("divergencias_do_mes", competencia="2026-09"))

    assert resposta.texto.endswith(f"{pendentes} estão sem justificativa.")
