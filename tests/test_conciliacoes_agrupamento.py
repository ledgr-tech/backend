"""Testes de integração do agrupamento N para 1 e 1 para N (issue #131,
ADR-027) contra Postgres real: o que `POST /conciliacoes` grava em
`conciliacoes` e `execucoes_conciliacao`, as contagens, a flag, a tolerância
da empresa e o isolamento por empresa. O núcleo puro está em
tests/test_matching_agrupamento.py, e as superfícies (listagem, CSV,
fechamento, relatório público, API pública, MCP) em
tests/test_agrupamento_superficies.py.
"""

import hashlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Lancamento
from main import app

client = TestClient(app)

DIA = date(2026, 9, 5)
LOTE_DE_150_MIL = [5000, 8000, 11000, 12500, 14000, 15500, 17000, 18500, 21000, 27500]


@pytest.fixture
def agrupamento_ligado(monkeypatch):
    monkeypatch.setattr(settings, "agrupamento_habilitado", True)


def inserir(db_session, extrato, itens):
    """Lançamentos em lote. `itens`: (valor, data, descricao). Cada item
    repetido recebe a próxima ocorrência, como faz a normalização (ADR-006)."""
    vistos: dict[tuple, int] = {}
    criados = []
    for valor, data, descricao in itens:
        vistos[(valor, data, descricao)] = vistos.get((valor, data, descricao), 0) + 1
        lancamento = Lancamento(
            empresa_id=extrato.empresa_id,
            extrato_id=extrato.id,
            data=data,
            valor=Decimal(valor),
            descricao=descricao,
            tipo="credito" if Decimal(valor) > 0 else "debito",
            hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            ocorrencia=vistos[(valor, data, descricao)],
        )
        db_session.add(lancamento)
        criados.append(lancamento)
    db_session.commit()
    return criados


def lote(valores, data=DIA, prefixo="Parcela"):
    return [(f"-{valor}.00", data, f"{prefixo} {indice}") for indice, valor in enumerate(valores)]


def conciliar(headers, banco, sistema):
    return client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )


def linhas_do_par(db_session, banco, sistema):
    db_session.expire_all()
    return (
        db_session.query(Conciliacao)
        .filter_by(extrato_banco_id=banco.id, extrato_sistema_id=sistema.id)
        .all()
    )


def execucoes_do_par(db_session, banco, sistema):
    db_session.expire_all()
    return (
        db_session.query(ExecucaoConciliacao)
        .filter_by(extrato_banco_id=banco.id, extrato_sistema_id=sistema.id)
        .all()
    )


@pytest.fixture
def par_de_extratos(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    """Empresa com um extrato do banco e um do sistema, já com os lançamentos."""

    def _montar(itens_banco, itens_sistema, tolerancia_dias=None, empresa=None):
        empresa = empresa or criar_empresa()
        if tolerancia_dias is not None:
            db_session.add(
                Configuracao(empresa_id=empresa.id, tolerancia_dias_default=tolerancia_dias)
            )
            db_session.commit()
        banco = criar_extrato_da_empresa(empresa.id, "banco")
        sistema = criar_extrato_da_empresa(empresa.id, "sistema")
        do_banco = inserir(db_session, banco, itens_banco)
        do_sistema = inserir(db_session, sistema, itens_sistema)
        headers = auth_headers(empresa.id)
        return empresa, banco, sistema, do_banco, do_sistema, headers

    return _montar


def test_lote_de_150_mil_grava_um_grupo_de_11_linhas_com_o_mesmo_grupo_id(
    db_session, par_de_extratos, agrupamento_ligado
):
    empresa, banco, sistema, do_banco, do_sistema, headers = par_de_extratos(
        [("-150000.00", DIA, "Pagamento em lote")], lote(LOTE_DE_150_MIL)
    )

    resposta = conciliar(headers, banco, sistema)

    assert resposta.status_code == 201
    assert resposta.json() == {
        "extrato_banco_id": str(banco.id),
        "extrato_sistema_id": str(sistema.id),
        "total": 11,
        "match_exato": 0,
        "match_tolerancia": 0,
        "match_agrupado": 11,
        "duplicado": 0,
        "sem_correspondencia": 0,
        "tarifa_bancaria": 0,
        "divergente_valor": 0,
        "divergente_data": 0,
        "agrupamentos": 1,
    }
    linhas = linhas_do_par(db_session, banco, sistema)
    assert len(linhas) == 11
    assert len({linha.grupo_id for linha in linhas}) == 1 and linhas[0].grupo_id is not None
    for linha in linhas:
        assert linha.empresa_id == empresa.id
        assert linha.status == "match_agrupado"
        assert linha.regra_aplicada == "agrupamento"
        assert linha.score_confianca == Decimal("0.900")
        assert (linha.lancamento_banco_id is None) != (linha.lancamento_sistema_id is None)
    assert {linha.lancamento_banco_id for linha in linhas} - {None} == {do_banco[0].id}
    assert {linha.lancamento_sistema_id for linha in linhas} - {None} == {i.id for i in do_sistema}


def test_contagens_em_linhas_e_grupos_somam_o_total_e_o_check_da_execucao_vale(
    db_session, par_de_extratos, agrupamento_ligado
):
    # um par exato, o lote agrupado e uma sobra sem par
    _, banco, sistema, _, _, headers = par_de_extratos(
        [("-10.00", DIA, "a"), ("-150000.00", DIA, "lote")],
        [("-10.00", DIA, "a")] + lote(LOTE_DE_150_MIL) + [("-3.33", DIA + timedelta(days=9), "x")],
    )

    corpo = conciliar(headers, banco, sistema).json()

    assert corpo["match_exato"] == 1
    assert corpo["match_agrupado"] == 11 and corpo["agrupamentos"] == 1
    assert corpo["sem_correspondencia"] == 1
    categorias = (
        "match_exato",
        "match_tolerancia",
        "match_agrupado",
        "duplicado",
        "sem_correspondencia",
        "tarifa_bancaria",
        "divergente_valor",
        "divergente_data",
    )
    assert corpo["total"] == sum(corpo[c] for c in categorias) == 13  # `agrupamentos` fica fora
    (execucao,) = execucoes_do_par(db_session, banco, sistema)
    assert execucao.match_agrupado == 11 and execucao.agrupamentos == 1
    assert execucao.total == corpo["total"]


def test_conciliar_duas_vezes_e_idempotente(db_session, par_de_extratos, agrupamento_ligado):
    _, banco, sistema, _, _, headers = par_de_extratos(
        [("-150000.00", DIA, "lote")], lote(LOTE_DE_150_MIL)
    )

    primeira = conciliar(headers, banco, sistema)
    ids_e_grupo = {
        (linha.id, linha.grupo_id) for linha in linhas_do_par(db_session, banco, sistema)
    }
    segunda = conciliar(headers, banco, sistema)

    assert primeira.json() == segunda.json()
    linhas = linhas_do_par(db_session, banco, sistema)
    assert len(linhas) == 11  # substituídas, não somadas
    assert len({linha.grupo_id for linha in linhas}) == 1
    assert not ids_e_grupo & {(linha.id, linha.grupo_id) for linha in linhas}
    assert len(execucoes_do_par(db_session, banco, sistema)) == 2  # o histórico só cresce


def test_isolamento_por_empresa_outra_empresa_nao_participa_do_grupo(
    db_session, criar_empresa, criar_extrato_da_empresa, par_de_extratos, agrupamento_ligado
):
    # a outra empresa tem, no extrato dela, os candidatos que fechariam o lote da primeira
    outra = criar_empresa()
    outro_banco = criar_extrato_da_empresa(outra.id, "banco")
    outro_sistema = criar_extrato_da_empresa(outra.id, "sistema")
    inserir(db_session, outro_sistema, lote(LOTE_DE_150_MIL))

    empresa, banco, sistema, _, _, headers = par_de_extratos(
        [("-150000.00", DIA, "lote")], lote(LOTE_DE_150_MIL[:5])
    )
    corpo = conciliar(headers, banco, sistema).json()

    assert corpo["agrupamentos"] == 0 and corpo["match_agrupado"] == 0
    assert not db_session.query(Conciliacao).filter_by(extrato_banco_id=outro_banco.id).all()

    # e, com os candidatos certos, o grupo só tem lançamentos da própria empresa
    _, banco2, sistema2, _, _, headers2 = par_de_extratos(
        [("-150000.00", DIA, "lote")], lote(LOTE_DE_150_MIL), empresa=empresa
    )
    assert conciliar(headers2, banco2, sistema2).json()["agrupamentos"] == 1
    for linha in linhas_do_par(db_session, banco2, sistema2):
        assert linha.empresa_id == empresa.id


def test_tolerancia_da_empresa_e_a_janela_do_grupo(db_session, par_de_extratos, agrupamento_ligado):
    espalhado = [
        ("-40.00", DIA - timedelta(days=2), "p1"),
        ("-30.00", DIA + timedelta(days=2), "p2"),
        ("-30.00", DIA, "p3"),
    ]
    # tolerância 0 (sem Configuracao): lançamentos de dias diferentes não agrupam
    _, banco0, sistema0, _, _, headers0 = par_de_extratos([("-100.00", DIA, "lote")], espalhado)
    sem_tolerancia = conciliar(headers0, banco0, sistema0).json()
    assert sem_tolerancia["agrupamentos"] == 0
    assert sem_tolerancia["divergente_valor"] >= 1

    # tolerância 2 na Configuracao da empresa: a mesma janela agrupa, com o score da fórmula
    _, banco2, sistema2, _, _, headers2 = par_de_extratos(
        [("-100.00", DIA, "lote")], espalhado, tolerancia_dias=2
    )
    corpo = conciliar(headers2, banco2, sistema2).json()
    assert corpo["agrupamentos"] == 1 and corpo["match_agrupado"] == 4
    assert {linha.score_confianca for linha in linhas_do_par(db_session, banco2, sistema2)} == {
        Decimal("0.333")
    }
    (execucao,) = execucoes_do_par(db_session, banco2, sistema2)
    assert execucao.tolerancia_dias == 2


def test_cenario_dos_dois_sentidos_de_ponta_a_ponta(
    db_session, par_de_extratos, agrupamento_ligado
):
    # o banco mostra o lote de 150 mil e três linhas (1k, 2k, 3k); o sistema tem as 10 linhas
    # do lote e um lançamento de 6k que o banco mostra em 3 linhas
    _, banco, sistema, _, _, headers = par_de_extratos(
        [("-150000.00", DIA, "lote")]
        + [(f"-{v}.00", DIA, f"linha {v}") for v in (1000, 2000, 3000)],
        lote(LOTE_DE_150_MIL) + [("-6000.00", DIA, "fornecedor")],
    )

    corpo = conciliar(headers, banco, sistema).json()

    assert corpo["total"] == corpo["match_agrupado"] == 15
    assert corpo["agrupamentos"] == 2
    linhas = linhas_do_par(db_session, banco, sistema)
    grupos: dict[uuid.UUID, list[Conciliacao]] = {}
    for linha in linhas:
        grupos.setdefault(linha.grupo_id, []).append(linha)
    assert sorted(len(g) for g in grupos.values()) == [4, 11]
    for grupo in grupos.values():
        bancos = [g for g in grupo if g.lancamento_banco_id]
        sistemas = [g for g in grupo if g.lancamento_sistema_id]
        assert (len(bancos), len(sistemas)) in {(1, 10), (3, 1)}


def test_flag_desligada_comportamento_identico_ao_de_antes(db_session, par_de_extratos):
    assert settings.agrupamento_habilitado is False
    _, banco, sistema, _, _, headers = par_de_extratos(
        [("-150000.00", DIA, "lote")], lote(LOTE_DE_150_MIL), tolerancia_dias=2
    )

    corpo = conciliar(headers, banco, sistema).json()

    assert corpo["match_agrupado"] == 0 and corpo["agrupamentos"] == 0
    assert corpo["divergente_valor"] == 11  # como o motor sempre fez
    linhas = linhas_do_par(db_session, banco, sistema)
    assert {linha.status for linha in linhas} == {"divergente_valor"}
    assert all(linha.grupo_id is None for linha in linhas)
    (execucao,) = execucoes_do_par(db_session, banco, sistema)
    assert execucao.match_agrupado == 0 and execucao.agrupamentos == 0


def test_um_para_um_tem_prioridade_sobre_o_grupo_no_banco(
    db_session, par_de_extratos, agrupamento_ligado
):
    # o pagamento individual de 11.000,00 consome o lançamento de 11.000,00 do lote: o grupo não fecha
    _, banco, sistema, _, _, headers = par_de_extratos(
        [("-150000.00", DIA, "lote"), ("-11000.00", DIA, "individual")], lote(LOTE_DE_150_MIL)
    )

    corpo = conciliar(headers, banco, sistema).json()

    assert corpo["match_agrupado"] == 0 and corpo["agrupamentos"] == 0
    assert corpo["match_exato"] == 1
    assert corpo["total"] == 11
