"""Testes de `montar_contexto_divergencia` (issue #28, ADR-011) — Postgres
real (fixtures de tests/conftest.py), sem rede: monta o `ContextoDivergencia`
a partir de linhas de `conciliacoes` inseridas diretamente, pra ter controle
fino sobre candidatos/ordem sem depender do motor de matching rodar de
verdade (esse já é coberto por tests/test_conciliacoes.py).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from test_conciliacoes import _lancamento_em

from app.models import Conciliacao, Configuracao, ExecucaoConciliacao
from app.services.ia.contexto import (
    FatosDoPar,
    calcular_fatos_do_par,
    cruza_fim_de_semana,
    montar_contexto_divergencia,
)
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


def test_par_de_data_usa_a_ponta_gravada_e_nao_vira_candidato_de_valor(db_session, par):
    empresa, banco, sistema = par
    dia = date(2026, 10, 5)
    do_banco = _lancamento_em(db_session, banco, "1854.00", dia)
    mesma_data = _lancamento_em(db_session, sistema, "957.72", dia)
    outra_data = _lancamento_em(db_session, sistema, "1854.00", date(2026, 10, 3))
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "divergente_data", do_banco.id, outra_data.id
    )
    sobra = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "divergente_valor", None, mesma_data.id
    )

    (candidato,) = montar_contexto_divergencia(db_session, linha).candidatos
    assert candidato.lancamento.data == outra_data.data
    assert candidato.diferenca_dias == 2
    assert montar_contexto_divergencia(db_session, sobra).candidatos == ()


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


# fatos calculados (fix da explicação detalhada)


def _execucao(db_session, empresa_id, banco_id, sistema_id, tolerancia, criado_em=None):
    execucao = ExecucaoConciliacao(
        empresa_id=empresa_id,
        extrato_banco_id=banco_id,
        extrato_sistema_id=sistema_id,
        tolerancia_dias=tolerancia,
        total=0,
        match_exato=0,
        match_tolerancia=0,
        duplicado=0,
        sem_correspondencia=0,
        tarifa_bancaria=0,
        divergente_valor=0,
        divergente_data=0,
    )
    if criado_em is not None:
        execucao.criado_em = criado_em
    db_session.add(execucao)
    db_session.commit()
    return execucao


def test_par_de_data_gravado_tem_fatos_calculados(db_session, par):
    empresa, banco, sistema = par
    # 02/10/2026 é sexta, 05/10/2026 é segunda: o intervalo cruza o fim de semana.
    do_banco = _lancamento_em(db_session, banco, "-79.61", date(2026, 10, 2), "PIX FORNECEDOR")
    do_sistema = _lancamento_em(db_session, sistema, "-79.61", date(2026, 10, 5), "PIX FORNECEDOR")
    _execucao(db_session, empresa.id, banco.id, sistema.id, 0)
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "divergente_data", do_banco.id, do_sistema.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.lado_do_item == "banco"
    assert contexto.tolerancia_dias == 0
    assert contexto.total_candidatos == 1
    assert contexto.par_gravado is True
    assert contexto.periodo_outro_lado == (date(2026, 10, 5), date(2026, 10, 5))
    (candidato,) = contexto.candidatos
    assert candidato.diferenca_dias == 3
    assert candidato.dias_assinados == 3
    assert candidato.cruza_fim_de_semana is True
    assert candidato.similaridade_descricao == 100
    assert candidato.diferenca_percentual is None


def test_divergente_data_sem_par_dias_assinados_nos_dois_sentidos(db_session, par):
    empresa, banco, sistema = par
    # 14/10/2026 é quarta; 13 (terça) e 15 (quinta) não cruzam fim de semana.
    item = _lancamento_em(db_session, banco, "50.00", date(2026, 10, 14), "Item")
    _lancamento_em(db_session, sistema, "50.00", date(2026, 10, 13), "Antes")
    _lancamento_em(db_session, sistema, "50.00", date(2026, 10, 16), "Depois")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "divergente_data", lancamento_banco_id=item.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.par_gravado is False
    assert [c.dias_assinados for c in contexto.candidatos] == [-1, 2]
    assert [c.diferenca_dias for c in contexto.candidatos] == [1, 2]
    assert [c.cruza_fim_de_semana for c in contexto.candidatos] == [False, False]


def test_divergente_valor_percentual_e_similaridade(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "-80.00", d, "TED JOAO SILVA")
    _lancamento_em(db_session, sistema, "-75.00", d, "JOAO SILVA")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    (candidato,) = contexto.candidatos
    assert candidato.diferenca_valor == Decimal("5.00")
    assert candidato.diferenca_percentual == Decimal("6.3")  # 5/80 = 6,25% -> 6,3
    assert candidato.similaridade_descricao == 100  # token_set_ratio
    assert candidato.dias_assinados is None
    assert candidato.cruza_fim_de_semana is None
    assert contexto.total_candidatos == 1


def test_total_candidatos_conta_antes_do_corte(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item")
    for valor in ("90.00", "91.00", "92.00", "93.00", "94.00"):
        _lancamento_em(db_session, sistema, valor, d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert len(contexto.candidatos) == 3
    assert contexto.total_candidatos == 5


def test_total_candidatos_nao_conta_os_ja_casados(db_session, par):
    empresa, banco, sistema = par
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item")
    ja_casado = _lancamento_em(db_session, sistema, "50.00", d, "Ja casado")
    _lancamento_em(db_session, sistema, "60.00", d, "Livre")
    outro_banco = _lancamento_em(db_session, banco, "50.00", d, "Par")
    _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "match_exato", outro_banco.id, ja_casado.id,
        regra_aplicada="exato", score_confianca=Decimal("1.000"),
    )  # fmt: skip
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.total_candidatos == 1
    assert [c.lancamento.valor for c in contexto.candidatos] == [Decimal("60.00")]


def test_tolerancia_vem_da_execucao_mais_recente(db_session, par):
    empresa, banco, sistema = par
    db_session.add(Configuracao(empresa_id=empresa.id, tolerancia_dias_default=7))
    db_session.commit()
    agora = datetime.now(UTC)
    _execucao(db_session, empresa.id, banco.id, sistema.id, 1, agora - timedelta(hours=2))
    _execucao(db_session, empresa.id, banco.id, sistema.id, 3, agora - timedelta(hours=1))
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )

    assert montar_contexto_divergencia(db_session, linha).tolerancia_dias == 3


def test_tolerancia_sem_execucao_vem_da_configuracao(db_session, par):
    empresa, banco, sistema = par
    db_session.add(Configuracao(empresa_id=empresa.id, tolerancia_dias_default=2))
    db_session.commit()
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )

    assert montar_contexto_divergencia(db_session, linha).tolerancia_dias == 2


def test_tolerancia_sem_execucao_nem_configuracao_e_none(db_session, par):
    empresa, banco, sistema = par
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )

    assert montar_contexto_divergencia(db_session, linha).tolerancia_dias is None


def test_execucao_de_outro_par_nao_vale(db_session, par, criar_extrato_da_empresa):
    empresa, banco, sistema = par
    outro_sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    _execucao(db_session, empresa.id, banco.id, outro_sistema.id, 5)
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )

    assert montar_contexto_divergencia(db_session, linha).tolerancia_dias is None


def test_sem_correspondencia_periodo_gravado_do_outro_lado(db_session, par):
    empresa, banco, sistema = par
    sistema.periodo_inicio = date(2026, 10, 1)
    sistema.periodo_fim = date(2026, 10, 31)
    db_session.commit()
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )

    contexto = montar_contexto_divergencia(db_session, linha)

    assert contexto.periodo_outro_lado == (date(2026, 10, 1), date(2026, 10, 31))
    assert contexto.candidatos == ()
    assert contexto.total_candidatos is None
    assert contexto.lado_do_item == "banco"


def test_periodo_sem_gravacao_usa_datas_dos_lancamentos_e_sem_nada_e_none(db_session, par):
    empresa, banco, sistema = par
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )
    assert montar_contexto_divergencia(db_session, linha).periodo_outro_lado is None

    _lancamento_em(db_session, sistema, "1.00", date(2026, 9, 3), "a")
    _lancamento_em(db_session, sistema, "2.00", date(2026, 9, 28), "b")
    assert montar_contexto_divergencia(db_session, linha).periodo_outro_lado == (
        date(2026, 9, 3),
        date(2026, 9, 28),
    )


def test_duplicado_e_tarifa_tem_lado_e_tolerancia(db_session, par):
    empresa, banco, sistema = par
    _execucao(db_session, empresa.id, banco.id, sistema.id, 1)
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, sistema, "10.00", d, "Item")
    _lancamento_em(db_session, sistema, "10.00", d, "Item 2")
    _lancamento_em(db_session, banco, "10.00", d, "Banco")
    tarifa = _lancamento_em(db_session, banco, "-15.00", d, "TARIFA PACOTE")
    duplicado = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "duplicado", None, item.id
    )
    linha_tarifa = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "tarifa_bancaria", tarifa.id
    )

    contexto = montar_contexto_divergencia(db_session, duplicado)
    assert contexto.lado_do_item == "sistema"
    assert contexto.tolerancia_dias == 1
    assert (contexto.quantidade_mesmo_lado, contexto.quantidade_outro_lado) == (2, 1)
    assert contexto.total_candidatos is None

    contexto = montar_contexto_divergencia(db_session, linha_tarifa)
    assert contexto.lado_do_item == "banco"
    assert contexto.tolerancia_dias == 1
    assert contexto.candidatos == ()


@pytest.mark.parametrize(
    ("data_a", "data_b", "esperado"),
    [
        (date(2026, 10, 2), date(2026, 10, 2), False),  # sexta, mesmo dia
        (date(2026, 10, 3), date(2026, 10, 3), True),  # sábado
        (date(2026, 10, 5), date(2026, 10, 2), True),  # segunda -> sexta, ordem invertida
        (date(2026, 10, 6), date(2026, 10, 9), False),  # terça a sexta
        (date(2026, 10, 1), date(2026, 10, 30), True),  # mais de uma semana
    ],
)
def test_cruza_fim_de_semana(data_a, data_b, esperado):
    assert cruza_fim_de_semana(data_a, data_b) is esperado


# FatosDoPar: o mesmo contexto com e sem o cálculo por par


def test_contexto_igual_com_e_sem_fatos_do_par_em_todos_os_status(db_session, par):
    empresa, banco, sistema = par
    sistema.periodo_inicio = date(2026, 9, 1)
    sistema.periodo_fim = date(2026, 9, 30)
    db_session.commit()
    _execucao(db_session, empresa.id, banco.id, sistema.id, 1)
    d = date(2026, 9, 21)
    data_b = _lancamento_em(db_session, banco, "-79.61", d, "PIX A")
    data_s = _lancamento_em(db_session, sistema, "-79.61", d + timedelta(days=3), "PIX A")
    valor_b = _lancamento_em(db_session, banco, "100.00", date(2026, 9, 10), "Item")
    _lancamento_em(db_session, sistema, "90.00", date(2026, 9, 10), "Outro")
    sem_b = _lancamento_em(db_session, banco, "7.00", date(2026, 10, 2), "Sem par")
    tarifa_b = _lancamento_em(db_session, banco, "-15.00", date(2026, 9, 11), "TARIFA")
    dup_s = _lancamento_em(db_session, sistema, "10.00", date(2026, 9, 12), "dup")
    _lancamento_em(db_session, sistema, "10.00", date(2026, 9, 12), "dup 2")
    _lancamento_em(db_session, banco, "10.00", date(2026, 9, 12), "dup banco")
    linhas = [
        _conciliacao(db_session, empresa.id, banco.id, sistema.id, "divergente_data",
                     data_b.id, data_s.id),
        _conciliacao(db_session, empresa.id, banco.id, sistema.id, "divergente_valor", valor_b.id),
        _conciliacao(db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia",
                     sem_b.id),
        _conciliacao(db_session, empresa.id, banco.id, sistema.id, "tarifa_bancaria",
                     tarifa_b.id),
        _conciliacao(db_session, empresa.id, banco.id, sistema.id, "duplicado", None, dup_s.id),
    ]  # fmt: skip

    fatos = calcular_fatos_do_par(db_session, empresa.id, banco.id, sistema.id)

    assert fatos.tolerancia_dias == 1
    assert fatos.periodo_sistema == (date(2026, 9, 1), date(2026, 9, 30))
    for linha in linhas:
        sem = montar_contexto_divergencia(db_session, linha)
        com = montar_contexto_divergencia(db_session, linha, fatos)
        assert com == sem, linha.status
        assert serializar_contexto(com) == serializar_contexto(sem)


def test_fatos_de_outro_par_sao_ignorados(db_session, par, criar_extrato_da_empresa):
    empresa, banco, sistema = par
    _execucao(db_session, empresa.id, banco.id, sistema.id, 2)
    outro = criar_extrato_da_empresa(empresa.id, "sistema")
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, "sem_correspondencia", item.id
    )
    errado = FatosDoPar(empresa.id, banco.id, outro.id, 5, None, None)

    assert montar_contexto_divergencia(db_session, linha, errado).tolerancia_dias == 2
