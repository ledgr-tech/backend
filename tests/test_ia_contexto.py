"""Testes de `montar_contexto_divergencia` (issue #28, ADR-011) — Postgres
real (fixtures de tests/conftest.py), sem rede: monta o `ContextoDivergencia`
a partir de linhas de `conciliacoes` inseridas diretamente, pra ter controle
fino sobre candidatos/ordem sem depender do motor de matching rodar de
verdade (esse já é coberto por tests/test_conciliacoes.py).
"""

from datetime import date
from decimal import Decimal

import pytest
from test_conciliacoes import _lancamento_em

from app.models import Conciliacao
from app.services.ia.contexto import montar_contexto_divergencia
from app.services.ia.prompt import serializar_contexto


def _conciliacao(
    db_session,
    empresa_id,
    extrato_banco_id,
    extrato_sistema_id,
    status,
    lancamento_banco_id=None,
    lancamento_sistema_id=None,
    regra_aplicada=None,
    score_confianca=None,
) -> Conciliacao:
    linha = Conciliacao(
        empresa_id=empresa_id,
        extrato_banco_id=extrato_banco_id,
        extrato_sistema_id=extrato_sistema_id,
        lancamento_banco_id=lancamento_banco_id,
        lancamento_sistema_id=lancamento_sistema_id,
        status=status,
        regra_aplicada=regra_aplicada,
        score_confianca=score_confianca,
    )
    db_session.add(linha)
    db_session.commit()
    return linha


@pytest.fixture
def par(db_session, criar_empresa, criar_extrato_da_empresa):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    return empresa, banco, sistema


def test_divergente_valor_ate_3_candidatos_mais_proximo_primeiro(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item do banco")
    _lancamento_em(db_session, sistema, "105.00", d, "Candidato longe")
    proximo = _lancamento_em(db_session, sistema, "99.00", d, "Candidato mais proximo")
    medio = _lancamento_em(db_session, sistema, "90.00", d, "Candidato meio")
    _lancamento_em(db_session, sistema, "200.00", d, "Candidato mais longe ainda")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.status == "divergente_valor"
    assert contexto.lancamento.valor == Decimal("100.00")
    assert contexto.quantidade_mesmo_lado is None
    assert contexto.quantidade_outro_lado is None
    assert len(contexto.candidatos) == 3
    diferencas = [c.diferenca_valor for c in contexto.candidatos]
    assert diferencas == sorted(diferencas)
    assert diferencas[0] == Decimal("1.00")  # 99.00
    assert contexto.candidatos[0].lancamento.descricao == proximo.descricao
    assert diferencas[1] == Decimal("5.00")  # 105.00
    assert diferencas[2] == Decimal("10.00")  # 90.00
    assert contexto.candidatos[2].lancamento.descricao == medio.descricao
    assert all(c.diferenca_dias is None for c in contexto.candidatos)
    # o 4º candidato (diferença 100.00) não entra
    assert Decimal("100.00") not in diferencas


def test_divergente_data_ate_3_mais_proxima_primeiro_em_dias(db_session, par):
    empresa, banco, sistema = par
    valor = "50.00"
    item = _lancamento_em(db_session, banco, valor, date(2026, 9, 20), "Item do banco")
    _lancamento_em(db_session, sistema, valor, date(2026, 9, 26), "6 dias depois")
    proximo = _lancamento_em(db_session, sistema, valor, date(2026, 9, 21), "1 dia depois")
    medio = _lancamento_em(db_session, sistema, valor, date(2026, 9, 17), "3 dias antes")
    _lancamento_em(db_session, sistema, valor, date(2026, 10, 20), "30 dias depois")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "divergente_data", lancamento_banco_id=item.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.status == "divergente_data"
    assert len(contexto.candidatos) == 3
    diferencas = [c.diferenca_dias for c in contexto.candidatos]
    assert diferencas == sorted(diferencas)
    assert diferencas[0] == 1
    assert contexto.candidatos[0].lancamento.descricao == proximo.descricao
    assert diferencas[1] == 3
    assert contexto.candidatos[1].lancamento.descricao == medio.descricao
    assert diferencas[2] == 6
    assert all(c.diferenca_valor is None for c in contexto.candidatos)
    assert 30 not in diferencas


def test_duplicado_quantidades_mesmo_lado_e_outro_lado(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "10.00", d, "Item 1")
    _lancamento_em(db_session, banco, "10.00", d, "Item 2")
    _lancamento_em(db_session, banco, "10.00", d, "Item 3")
    _lancamento_em(db_session, sistema, "10.00", d, "Sistema unico")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "duplicado", lancamento_banco_id=item.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.status == "duplicado"
    assert contexto.candidatos == ()
    assert contexto.quantidade_mesmo_lado == 3
    assert contexto.quantidade_outro_lado == 1


@pytest.mark.parametrize("status", ["tarifa_bancaria", "sem_correspondencia"])
def test_tarifa_e_sem_correspondencia_sem_candidatos(db_session, par, status):
    empresa, banco, sistema = par
    item = _lancamento_em(db_session, banco, "15.00", date(2026, 9, 20), "TARIFA PACOTE SERVICOS")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, status, lancamento_banco_id=item.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.status == status
    assert contexto.candidatos == ()
    assert contexto.quantidade_mesmo_lado is None
    assert contexto.quantidade_outro_lado is None


def test_exclui_candidatos_ja_casados_por_match_exato(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item divergente")
    ja_casado = _lancamento_em(db_session, sistema, "50.00", d, "Ja tem par confirmado")
    candidato_livre = _lancamento_em(db_session, sistema, "60.00", d, "Ainda solto")
    outro_banco = _lancamento_em(db_session, banco, "50.00", d, "Par do ja_casado")
    _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "match_exato",
        lancamento_banco_id=outro_banco.id,
        lancamento_sistema_id=ja_casado.id,
        regra_aplicada="exato",
        score_confianca=Decimal("1.000"),
    )
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    descricoes = {c.lancamento.descricao for c in contexto.candidatos}
    assert candidato_livre.descricao in descricoes
    assert ja_casado.descricao not in descricoes


def test_exclui_candidatos_ja_casados_por_match_tolerancia(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item divergente")
    ja_casado = _lancamento_em(db_session, sistema, "50.00", d, "Ja tem par por tolerancia")
    outro_banco = _lancamento_em(
        db_session, banco, "50.00", date(2026, 9, 21), "Par por tolerancia"
    )
    _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "match_tolerancia",
        lancamento_banco_id=outro_banco.id,
        lancamento_sistema_id=ja_casado.id,
        regra_aplicada="tolerancia",
        score_confianca=Decimal("0.667"),
    )
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    descricoes = {c.lancamento.descricao for c in contexto.candidatos}
    assert ja_casado.descricao not in descricoes


def test_contexto_nao_inclui_id_de_lancamento(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item")
    candidato = _lancamento_em(db_session, sistema, "99.00", d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)
    serializado = serializar_contexto(contexto)

    assert str(item.id) not in serializado
    assert str(candidato.id) not in serializado
    assert not hasattr(contexto.lancamento, "id")
    assert not hasattr(contexto.candidatos[0].lancamento, "id")


def test_mesma_entrada_gera_mesma_serializacao_duas_execucoes(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item")
    _lancamento_em(db_session, sistema, "99.00", d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    primeira = serializar_contexto(montar_contexto_divergencia(db_session, linha))
    segunda = serializar_contexto(montar_contexto_divergencia(db_session, linha))

    assert primeira == segunda


@pytest.mark.parametrize("status", ["match_exato", "match_tolerancia", "status_desconhecido"])
def test_status_nao_elegivel_levanta_value_error(db_session, par, status):
    empresa, banco, sistema = par
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        status if status != "status_desconhecido" else "sem_correspondencia",
        lancamento_banco_id=item.id,
    )
    # Bypassa o CHECK de status válido só pra simular "status_desconhecido"
    # chegando na função (defesa em profundidade — nunca ocorre via API real).
    # expunge tira o objeto da sessão antes da mutação: sem isso, a mudança
    # em memória fica "dirty" e o commit da limpeza de criar_empresa tenta
    # dar flush numa linha que a própria limpeza já apagou (ObjectDeletedError).
    if status == "status_desconhecido":
        db_session.expunge(linha)
        linha.status = "status_desconhecido"

    with pytest.raises(ValueError):
        montar_contexto_divergencia(db_session, linha)


def test_item_do_lado_sistema_tambem_funciona(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, sistema, "100.00", d, "Item do sistema")
    _lancamento_em(db_session, banco, "99.00", d, "Candidato do banco")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_sistema_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.lancamento.descricao == "Item do sistema"
    assert len(contexto.candidatos) == 1
    assert contexto.candidatos[0].lancamento.descricao == "Candidato do banco"
