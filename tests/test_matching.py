"""Testes unitários do núcleo puro do motor de matching exato (issue #16,
ADR-006), da passada de tolerância de data (issue #22, ADR-008) e do
desempate de pareamento por similaridade de descrição (issue #23, ADR-008).
Sem banco: só listas em memória."""

import uuid
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz

from app.services.matching import (
    LIMITE_CANDIDATOS_PARA_SIMILARIDADE,
    SCORE_EXATO,
    LancamentoParaMatching,
    ResultadoMatching,
    _similaridade_descricao,
    parear_lancamentos,
    parear_por_tolerancia,
)

DIA = date(2026, 9, 5)


def _lanc(valor: str, data: date = DIA, descricao: str = "Pagamento", ocorrencia: int = 1):
    return LancamentoParaMatching(
        id=uuid.uuid4(),
        valor=Decimal(valor),
        data=data,
        descricao=descricao,
        ocorrencia=ocorrencia,
    )


def _por_status(resultados: list[ResultadoMatching]) -> dict[str, list[ResultadoMatching]]:
    agrupado: dict[str, list[ResultadoMatching]] = {}
    for resultado in resultados:
        agrupado.setdefault(resultado.status, []).append(resultado)
    return agrupado


def test_sobreposicao_simples_gera_n_matches_e_resto_sem_correspondencia():
    banco = [_lanc("10.00"), _lanc("20.00"), _lanc("30.00"), _lanc("99.00")]
    sistema = [_lanc("10.00"), _lanc("20.00"), _lanc("30.00"), _lanc("77.00")]

    por_status = _por_status(parear_lancamentos(banco, sistema))

    assert len(por_status["match_exato"]) == 3
    assert len(por_status["sem_correspondencia"]) == 2
    assert "duplicado" not in por_status
    for match in por_status["match_exato"]:
        assert match.regra_aplicada == "exato"
        assert match.score_confianca == SCORE_EXATO == Decimal("1.000")
        assert match.lancamento_banco_id is not None
        assert match.lancamento_sistema_id is not None
    lados = {
        (r.lancamento_banco_id is None, r.lancamento_sistema_id is None)
        for r in por_status["sem_correspondencia"]
    }
    assert lados == {(False, True), (True, False)}
    for sem_par in por_status["sem_correspondencia"]:
        assert sem_par.regra_aplicada is None
        assert sem_par.score_confianca is None


def test_grupo_com_3_do_banco_e_2_do_sistema_gera_2_matches_e_1_duplicado_do_banco():
    banco = [_lanc("150.00", ocorrencia=o) for o in (1, 2, 3)]
    sistema = [_lanc("150.00", ocorrencia=o) for o in (1, 2)]

    por_status = _por_status(parear_lancamentos(banco, sistema))

    assert len(por_status["match_exato"]) == 2
    assert len(por_status["duplicado"]) == 1
    duplicado = por_status["duplicado"][0]
    assert duplicado.lancamento_banco_id is not None
    assert duplicado.lancamento_sistema_id is None
    assert duplicado.lancamento_banco_id == banco[2].id  # o excedente é o último na ordenação


def test_grupo_com_2_do_sistema_e_1_do_banco_gera_1_match_e_1_duplicado_do_sistema():
    banco = [_lanc("150.00")]
    sistema = [_lanc("150.00", ocorrencia=1), _lanc("150.00", ocorrencia=2)]

    por_status = _por_status(parear_lancamentos(banco, sistema))

    assert len(por_status["match_exato"]) == 1
    assert len(por_status["duplicado"]) == 1
    duplicado = por_status["duplicado"][0]
    assert duplicado.lancamento_banco_id is None
    assert duplicado.lancamento_sistema_id == sistema[1].id


def test_grupo_com_2_do_banco_e_nenhum_do_sistema_gera_2_sem_correspondencia():
    banco = [_lanc("150.00", ocorrencia=1), _lanc("150.00", ocorrencia=2)]

    resultados = parear_lancamentos(banco, [])

    assert [r.status for r in resultados] == ["sem_correspondencia"] * 2
    assert all(r.lancamento_sistema_id is None for r in resultados)


def test_mesmo_valor_com_sinal_oposto_nao_casa():
    resultados = parear_lancamentos([_lanc("150.00")], [_lanc("-150.00")])

    assert sorted(r.status for r in resultados) == ["sem_correspondencia"] * 2


def test_datas_diferentes_nao_casam():
    resultados = parear_lancamentos(
        [_lanc("10.00", date(2026, 9, 5))], [_lanc("10.00", date(2026, 9, 6))]
    )

    assert [r.status for r in resultados] == ["sem_correspondencia"] * 2


def test_pareamento_ordena_por_descricao_normalizada_e_ocorrencia():
    """Descrições idênticas (a menos de espaço/caixa) empatam a similaridade
    em 100 dos dois lados — quem decide o pareamento é o desempate de sempre
    (descrição normalizada, ocorrência), não a similaridade (issue #23)."""
    b_a = _lanc("50.00", descricao="  Alfa ")
    b_b = _lanc("50.00", descricao="Beta")
    s_a = _lanc("50.00", descricao="ALFA")
    s_b = _lanc("50.00", descricao="beta")

    resultados = parear_lancamentos([b_b, b_a], [s_b, s_a])

    pares = {(r.lancamento_banco_id, r.lancamento_sistema_id) for r in resultados}
    assert pares == {(b_a.id, s_a.id), (b_b.id, s_b.id)}


def test_mesma_entrada_duas_vezes_gera_o_mesmo_pareamento():
    banco = [_lanc("150.00", ocorrencia=o) for o in (1, 2, 3)] + [_lanc("10.00", descricao="x")]
    sistema = [_lanc("150.00", ocorrencia=o) for o in (1, 2)] + [_lanc("5.00")]

    primeira = parear_lancamentos(banco, sistema)
    segunda = parear_lancamentos(list(banco), list(sistema))
    invertida = parear_lancamentos(list(reversed(banco)), list(reversed(sistema)))

    assert primeira == segunda
    assert primeira == invertida


def test_tolerancia_par_dentro_da_janela_casa_com_score_esperado():
    banco = _lanc("10.00", data=date(2026, 9, 5))
    sistema = _lanc("10.00", data=date(2026, 9, 6))

    resultados = parear_por_tolerancia([banco], [sistema], 2)

    assert resultados == [
        ResultadoMatching(
            status="match_tolerancia",
            lancamento_banco_id=banco.id,
            lancamento_sistema_id=sistema.id,
            regra_aplicada="tolerancia",
            score_confianca=Decimal("0.667"),
        )
    ]


def test_tolerancia_par_fora_da_janela_nao_casa():
    banco = _lanc("10.00", data=date(2026, 9, 5))
    sistema = _lanc("10.00", data=date(2026, 9, 9))

    resultados = parear_por_tolerancia([banco], [sistema], 2)

    assert {r.status for r in resultados} == {"sem_correspondencia"}
    assert len(resultados) == 2


def test_tolerancia_zero_nao_casa_nada():
    banco = _lanc("10.00")
    sistema = _lanc("10.00", data=date(2026, 9, 6))

    resultados = parear_por_tolerancia([banco], [sistema], 0)

    assert {r.status for r in resultados} == {"sem_correspondencia"}
    assert len(resultados) == 2


def test_tolerancia_valor_diferente_nao_casa():
    resultados = parear_por_tolerancia([_lanc("10.00")], [_lanc("11.00")], 3)

    assert {r.status for r in resultados} == {"sem_correspondencia"}


def test_tolerancia_com_lado_vazio_devolve_sem_correspondencia():
    banco = _lanc("10.00")

    resultados = parear_por_tolerancia([banco], [], 2)

    assert resultados == [ResultadoMatching("sem_correspondencia", banco.id, None)]


def test_tolerancia_desempate_menor_diferenca_de_dias_ganha():
    banco = _lanc("10.00", data=date(2026, 9, 5))
    longe = _lanc("10.00", data=date(2026, 9, 7))
    perto = _lanc("10.00", data=date(2026, 9, 6))

    resultados = parear_por_tolerancia([banco], [longe, perto], 3)

    pares = [r for r in resultados if r.status == "match_tolerancia"]
    assert [(r.lancamento_banco_id, r.lancamento_sistema_id) for r in pares] == [
        (banco.id, perto.id)
    ]
    assert pares[0].score_confianca == Decimal("0.750")
    sobras = [r for r in resultados if r.status == "sem_correspondencia"]
    assert [r.lancamento_sistema_id for r in sobras] == [longe.id]


def test_tolerancia_mesma_entrada_duas_vezes_e_invertida_geram_o_mesmo_pareamento():
    banco = [
        _lanc("10.00", data=date(2026, 9, 5), descricao="a"),
        _lanc("10.00", data=date(2026, 9, 5), descricao="b"),
        _lanc("20.00", data=date(2026, 9, 6)),
    ]
    sistema = [
        _lanc("10.00", data=date(2026, 9, 6), descricao="a"),
        _lanc("10.00", data=date(2026, 9, 6), descricao="b"),
        _lanc("20.00", data=date(2026, 9, 6)),
    ]

    def _pares(resultados):
        return {
            (r.lancamento_banco_id, r.lancamento_sistema_id, r.status, r.score_confianca)
            for r in resultados
        }

    primeira = parear_por_tolerancia(banco, sistema, 2)
    segunda = parear_por_tolerancia(list(banco), list(sistema), 2)
    invertida = parear_por_tolerancia(list(reversed(banco)), list(reversed(sistema)), 2)

    assert primeira == segunda
    assert _pares(primeira) == _pares(invertida)


def test_similaridade_descricao_usa_o_maior_entre_token_sort_e_token_set_ratio():
    # token_sort_ratio ganha: mesmas palavras, ordem diferente, nenhuma sobra
    # de um lado só (os dois ratios seriam iguais aqui, mas o cenário abaixo
    # de token_set isola o outro caso).
    assert _similaridade_descricao("TED MARIA OLIVEIRA", "OLIVEIRA MARIA TED") == 100.0

    # token_set_ratio ganha: sistema é um subconjunto de palavras do banco
    # (truncamento), então token_sort_ratio sozinho ficaria bem abaixo de 100.
    banco = "PIX RECEBIDO JOAO DA SILVA"
    sistema = "JOAO SILVA"
    assert _similaridade_descricao(banco, sistema) == 100.0
    assert fuzz.token_sort_ratio(banco, sistema) < 100.0


def test_grupo_com_2_de_cada_lado_pareia_por_similaridade_nao_por_ordem_alfabetica():
    # Construído pra que o desempate alfabético de hoje (descrição
    # normalizada) escolheria o par errado: "maria oliveira ted" <
    # "pix recebido..." e "joao silva" < "oliveira maria ted", então a ordem
    # posicional pareia banco_1-sistema_a e banco_2-sistema_b — os DOIS pares
    # errados. A similaridade (token_sort/token_set) pareia certo mesmo
    # assim: banco_1 com sistema_b (mesmas palavras, ordem diferente) e
    # banco_2 com sistema_a (sistema é a descrição truncada do banco).
    banco_1 = _lanc("50.00", descricao="MARIA OLIVEIRA TED")
    banco_2 = _lanc("50.00", descricao="PIX RECEBIDO JOAO DA SILVA")
    sistema_a = _lanc("50.00", descricao="JOAO SILVA")
    sistema_b = _lanc("50.00", descricao="OLIVEIRA MARIA TED")

    resultados = parear_lancamentos([banco_1, banco_2], [sistema_a, sistema_b])

    pares = {(r.lancamento_banco_id, r.lancamento_sistema_id) for r in resultados}
    assert pares == {(banco_1.id, sistema_b.id), (banco_2.id, sistema_a.id)}
    assert all(r.status == "match_exato" for r in resultados)


def test_tolerancia_desempate_por_similaridade_quando_diferenca_de_dias_empata():
    banco = _lanc("10.00", data=date(2026, 9, 5), descricao="PIX RECEBIDO JOAO DA SILVA")
    # Os dois candidatos ficam a 1 dia de diferença do banco (empate na
    # diferença de dias) — só a similaridade de descrição distingue.
    errado = _lanc("10.00", data=date(2026, 9, 6), descricao="MARIA OLIVEIRA")
    certo = _lanc("10.00", data=date(2026, 9, 4), descricao="JOAO SILVA")

    resultados = parear_por_tolerancia([banco], [errado, certo], 3)

    pares = [r for r in resultados if r.status == "match_tolerancia"]
    assert [(r.lancamento_banco_id, r.lancamento_sistema_id) for r in pares] == [
        (banco.id, certo.id)
    ]
    assert pares[0].score_confianca == Decimal("0.750")
    sobras = [r for r in resultados if r.status == "sem_correspondencia"]
    assert [r.lancamento_sistema_id for r in sobras] == [errado.id]


def test_grupo_acima_do_limite_de_candidatos_volta_pro_pareamento_posicional():
    # Descrições desenhadas pra que a similaridade escolheria um pareamento
    # diferente do posicional (mesmo truque do teste acima) — construído
    # LIMITE_CANDIDATOS_PARA_SIMILARIDADE + 1 vezes de cada lado pra estourar
    # o limite e confirmar que o motor não tenta comparar todas as
    # combinações nesse caso (custo O(n·m) não escala, issue #19 x #23).
    tamanho = LIMITE_CANDIDATOS_PARA_SIMILARIDADE + 1
    banco = [
        _lanc("50.00", descricao="MARIA OLIVEIRA TED", ocorrencia=o) for o in range(1, tamanho + 1)
    ]
    sistema = [_lanc("50.00", descricao="JOAO SILVA", ocorrencia=o) for o in range(1, tamanho + 1)]

    resultados = parear_lancamentos(banco, sistema)

    # Posicional: banco[i] casa com sistema[i] na ordem de (descrição
    # normalizada, ocorrência) — como as descrições são fixas por lado, a
    # ordem vira só a ocorrência.
    pares = {(r.lancamento_banco_id, r.lancamento_sistema_id) for r in resultados}
    esperado = {(b.id, s.id) for b, s in zip(banco, sistema, strict=True)}
    assert pares == esperado
    assert all(r.status == "match_exato" for r in resultados)
