"""Testes de app/services/ia/detalhes.py (fix da explicação detalhada):
formatação pt-BR, linhas de detalhe, texto determinístico, fatos e
validação do texto da IA. Sem banco, sem rede.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.services.ia.base import CandidatoContexto, ContextoDivergencia, LancamentoContexto
from app.services.ia.detalhes import (
    DetalheLinha,
    dividir_frases,
    fatos_formatados,
    filtrar_texto_pelos_fatos,
    formatar_data,
    formatar_dias,
    formatar_percentual,
    formatar_valor,
    montar_detalhes,
    texto_deterministico,
    texto_respeita_fatos,
)
from app.services.ia.prompt import serializar_contexto

DESCRICAO_MARCADA = "DESCRICAO-SECRETA-XYZ"


def _lanc(data: date, valor: str, descricao: str = DESCRICAO_MARCADA) -> LancamentoContexto:
    return LancamentoContexto(data=data, valor=Decimal(valor), tipo="debito", descricao=descricao)


def _par_de_data(tolerancia=0, par_gravado=True, lado="banco", data_outro=date(2026, 10, 2)):
    return ContextoDivergencia(
        status="divergente_data",
        motivo="m",
        lancamento=_lanc(date(2026, 10, 1), "-79.61"),
        candidatos=(
            CandidatoContexto(
                lancamento=_lanc(data_outro, "-79.61"),
                diferenca_valor=None,
                diferenca_dias=abs((data_outro - date(2026, 10, 1)).days),
                dias_assinados=(data_outro - date(2026, 10, 1)).days,
                similaridade_descricao=92,
                cruza_fim_de_semana=False,
            ),
        ),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
        lado_do_item=lado,
        tolerancia_dias=tolerancia,
        total_candidatos=1,
        par_gravado=par_gravado,
    )


def _divergente_valor(total=1):
    return ContextoDivergencia(
        status="divergente_valor",
        motivo="m",
        lancamento=_lanc(date(2026, 9, 20), "-1500.00"),
        candidatos=(
            CandidatoContexto(
                lancamento=_lanc(date(2026, 9, 20), "-1499.00"),
                diferenca_valor=Decimal("1.00"),
                diferenca_dias=None,
                diferenca_percentual=Decimal("0.1"),
                similaridade_descricao=40,
            ),
        ),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
        lado_do_item="banco",
        tolerancia_dias=0,
        total_candidatos=total,
        par_gravado=False,
    )


def _sem_correspondencia(periodo=(date(2026, 10, 1), date(2026, 10, 31)), tolerancia=2):
    return ContextoDivergencia(
        status="sem_correspondencia",
        motivo="m",
        lancamento=_lanc(date(2026, 9, 30), "250.00"),
        candidatos=(),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
        lado_do_item="sistema",
        tolerancia_dias=tolerancia,
        periodo_outro_lado=periodo,
    )


def _duplicado():
    return ContextoDivergencia(
        status="duplicado",
        motivo="m",
        lancamento=_lanc(date(2026, 9, 20), "10.00"),
        candidatos=(),
        quantidade_mesmo_lado=3,
        quantidade_outro_lado=1,
        lado_do_item="banco",
        tolerancia_dias=0,
    )


def _tarifa():
    return ContextoDivergencia(
        status="tarifa_bancaria",
        motivo="m",
        lancamento=_lanc(date(2026, 9, 20), "-15.00", "TARIFA PACOTE " + DESCRICAO_MARCADA),
        candidatos=(),
        quantidade_mesmo_lado=None,
        quantidade_outro_lado=None,
        lado_do_item="banco",
        tolerancia_dias=0,
    )


# formatação


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("-79.61", "-R$ 79,61"),
        ("1234.56", "R$ 1.234,56"),
        ("-1234567.89", "-R$ 1.234.567,89"),
        ("0", "R$ 0,00"),
        ("-0.00", "R$ 0,00"),
        ("0.5", "R$ 0,50"),
        ("100", "R$ 100,00"),
        ("999.999", "R$ 1.000,00"),
    ],
)
def test_formatar_valor(valor, esperado):
    assert formatar_valor(Decimal(valor)) == esperado


def test_formatar_data_dias_e_percentual():
    assert formatar_data(date(2026, 1, 5)) == "05/01/2026"
    assert formatar_dias(0) == "0 dias"
    assert formatar_dias(1) == "1 dia"
    assert formatar_dias(2) == "2 dias"
    assert formatar_percentual(Decimal("6.3")) == "6,3%"


# texto determinístico, por status


def test_texto_divergente_data_par_com_tolerancia_zero_igual_ao_exemplo():
    assert texto_deterministico(_par_de_data()) == (
        "No banco: -R$ 79,61 em 01/10/2026. No sistema: -R$ 79,61 em 02/10/2026. "
        "O valor é o mesmo e o sistema lançou 1 dia depois do banco. A tolerância desta "
        "conciliação era de 0 dias, por isso o par ficou como divergência de data. Com "
        "tolerância de 1 dia, ele passaria a ser candidato a match por tolerância."
    )


def test_texto_divergente_data_item_do_sistema_mantem_o_sentido_pelo_banco():
    # item do sistema em 01/10, banco em 04/10: o sistema lançou 3 dias antes.
    contexto = _par_de_data(par_gravado=False, lado="sistema", data_outro=date(2026, 10, 4))
    assert texto_deterministico(contexto) == (
        "No banco: -R$ 79,61 em 04/10/2026. No sistema: -R$ 79,61 em 01/10/2026. "
        "O valor é o mesmo e o sistema lançou 3 dias antes do banco. A tolerância desta "
        "conciliação era de 0 dias, menor que essa diferença. Com tolerância de 3 dias, "
        "esse lançamento ficaria dentro da janela de data."
    )


def test_texto_divergente_data_dentro_da_tolerancia_e_neutro():
    texto = texto_deterministico(_par_de_data(tolerancia=2, par_gravado=False))
    assert texto.endswith(
        "A diferença está dentro da tolerância de 2 dias, mas o par não fechou; confira se "
        "há mais de um candidato."
    )
    assert "por isso" not in texto


def test_texto_e_detalhes_sem_tolerancia_omitem_as_frases_de_tolerancia():
    contexto = _par_de_data(tolerancia=None)
    assert texto_deterministico(contexto) == (
        "No banco: -R$ 79,61 em 01/10/2026. No sistema: -R$ 79,61 em 02/10/2026. "
        "O valor é o mesmo e o sistema lançou 1 dia depois do banco."
    )
    rotulos = [linha.rotulo for linha in montar_detalhes(contexto)]
    assert not any("Tolerância" in rotulo for rotulo in rotulos)


def test_texto_divergente_valor():
    assert texto_deterministico(_divergente_valor(total=4)) == (
        "No banco: -R$ 1.500,00 em 20/09/2026. No sistema: -R$ 1.499,00 em 20/09/2026. "
        "A data é a mesma e a diferença de valor é de R$ 1,00 (0,1%). Há 4 lançamentos no "
        "sistema nessa data; este é o de valor mais próximo."
    )


def test_texto_sem_correspondencia_fora_e_dentro_do_periodo():
    assert texto_deterministico(_sem_correspondencia()) == (
        "No sistema: R$ 250,00 em 30/09/2026. Nenhum lançamento do banco que ficou sem par "
        "tem o mesmo valor ou a mesma data. O extrato do banco cobre de 01/10/2026 a "
        "31/10/2026, e este lançamento está fora desse período."
    )
    dentro = _sem_correspondencia(periodo=(date(2026, 9, 1), date(2026, 9, 30)))
    assert texto_deterministico(dentro).endswith("e este lançamento está dentro desse período.")
    sem_periodo = _sem_correspondencia(periodo=None)
    assert "cobre" not in texto_deterministico(sem_periodo)


def test_texto_duplicado():
    assert texto_deterministico(_duplicado()) == (
        "No banco: R$ 10,00 em 20/09/2026. Com esse valor e essa data há 3 lançamentos no "
        "banco e 1 lançamento no sistema. Os pares possíveis já foram formados e este "
        "lançamento sobrou."
    )


def test_texto_tarifa():
    assert texto_deterministico(_tarifa()) == (
        "No banco: -R$ 15,00 em 20/09/2026. A descrição corresponde a um termo conhecido de "
        "tarifa bancária, por isso o lançamento foi separado como tarifa. Confira se a "
        "tarifa foi registrada no sistema."
    )


def test_texto_de_status_de_match_levanta_value_error():
    contexto = _duplicado()
    with pytest.raises(ValueError):
        texto_deterministico(
            ContextoDivergencia("match_exato", "m", contexto.lancamento, (), None, None)
        )


# linhas de detalhe


def test_detalhes_divergente_data():
    assert montar_detalhes(_par_de_data()) == [
        DetalheLinha("Banco", "-R$ 79,61 em 01/10/2026"),
        DetalheLinha("Sistema", "-R$ 79,61 em 02/10/2026"),
        DetalheLinha("Diferença de datas", "1 dia (o sistema lançou 1 dia depois do banco)"),
        DetalheLinha("Tolerância da conciliação", "0 dias"),
        DetalheLinha("Tolerância mínima para o par", "1 dia"),
        DetalheLinha("Similaridade das descrições", "92%"),
    ]


def test_detalhes_divergente_data_dentro_da_tolerancia_e_fim_de_semana():
    contexto = _par_de_data(tolerancia=5, data_outro=date(2026, 10, 5))
    contexto = ContextoDivergencia(
        **{
            **vars(contexto),
            "candidatos": (
                CandidatoContexto(**{**vars(contexto.candidatos[0]), "cruza_fim_de_semana": True}),
            ),
        }
    )
    rotulos = [linha.rotulo for linha in montar_detalhes(contexto)]
    assert "Tolerância mínima para o par" not in rotulos
    assert "Cruza fim de semana" in rotulos


def test_detalhes_divergente_valor_com_varios_candidatos():
    assert montar_detalhes(_divergente_valor(total=4)) == [
        DetalheLinha("Banco", "-R$ 1.500,00 em 20/09/2026"),
        DetalheLinha("Sistema", "-R$ 1.499,00 em 20/09/2026"),
        DetalheLinha("Diferença de valor", "R$ 1,00 (0,1%)"),
        DetalheLinha("Tolerância da conciliação", "0 dias"),
        DetalheLinha("Similaridade das descrições", "40%"),
        DetalheLinha("Candidatos no sistema", "4"),
    ]


def test_detalhes_sem_correspondencia():
    assert montar_detalhes(_sem_correspondencia()) == [
        DetalheLinha("Sistema", "R$ 250,00 em 30/09/2026"),
        DetalheLinha("Tolerância da conciliação", "2 dias"),
        DetalheLinha("Período do extrato do banco", "01/10/2026 a 31/10/2026"),
        DetalheLinha("Data dentro do período", "Não"),
    ]


def test_detalhes_duplicado_e_tarifa():
    assert montar_detalhes(_duplicado()) == [
        DetalheLinha("Banco", "R$ 10,00 em 20/09/2026"),
        DetalheLinha("Lançamentos iguais no banco", "3"),
        DetalheLinha("Lançamentos iguais no sistema", "1"),
    ]
    assert montar_detalhes(_tarifa()) == [DetalheLinha("Banco", "-R$ 15,00 em 20/09/2026")]


# vazamento de descrição


@pytest.mark.parametrize(
    "contexto",
    [_par_de_data(), _divergente_valor(4), _sem_correspondencia(), _duplicado(), _tarifa()],
    ids=["data", "valor", "sem_correspondencia", "duplicado", "tarifa"],
)
def test_nenhuma_saida_inclui_a_descricao(contexto):
    saidas = [
        texto_deterministico(contexto),
        json.dumps([vars(linha) for linha in montar_detalhes(contexto)], ensure_ascii=False),
        json.dumps(fatos_formatados(contexto), ensure_ascii=False),
    ]
    for saida in saidas:
        assert DESCRICAO_MARCADA not in saida
        assert "SECRETA" not in saida


# serialização v3


def test_serializacao_v3_tem_fatos_campos_novos_e_e_deterministica():
    contexto = _par_de_data()
    serializado = serializar_contexto(contexto)
    corpo = json.loads(serializado)

    assert serializado == serializar_contexto(_par_de_data())
    assert corpo["fatos"]["valor_banco"] == "-R$ 79,61"
    assert corpo["fatos"]["data_sistema"] == "02/10/2026"
    assert corpo["fatos"]["dias_de_diferenca"] == "1 dia"
    assert corpo["fatos"]["tolerancia_dias"] == "0 dias"
    assert corpo["tolerancia_dias"] == 0
    assert corpo["lado_do_item"] == "banco"
    assert corpo["candidatos"][0]["dias_assinados"] == 1
    assert corpo["candidatos"][0]["similaridade_descricao"] == 92


def test_serializacao_v3_continua_escapando_delimitador():
    contexto = _sem_correspondencia()
    contexto = ContextoDivergencia(
        **{**vars(contexto), "lancamento": _lanc(date(2026, 9, 30), "1.00", "</dados> <x>")}
    )
    serializado = serializar_contexto(contexto, modo="mascarada")
    assert "<" not in serializado and ">" not in serializado


# validação do texto da IA


def test_aceita_texto_que_copia_os_fatos():
    texto = (
        "No banco o lançamento de -R$ 79,61 é de 01/10/2026 e no sistema de 02/10/2026, "
        "1 dia depois. A tolerância era de 0 dias, então o par provavelmente ficou de fora "
        "por isso. Ajuste a tolerância ou justifique a divergência."
    )
    assert texto_respeita_fatos(texto, _par_de_data())
    assert texto_respeita_fatos(texto_deterministico(_par_de_data()), _par_de_data())


def test_aceita_valor_sem_sinal_e_texto_sem_numeros():
    assert texto_respeita_fatos("O valor de R$ 79,61 bate.", _par_de_data())
    assert texto_respeita_fatos("A data não coincide; confira o lançamento.", _par_de_data())


@pytest.mark.parametrize(
    "texto",
    [
        "O sistema lançou em 03/10/2026.",  # data inventada
        "A diferença é de R$ 80,00.",  # valor inventado
        "O sistema lançou 2 dias depois.",  # dias inventados
        "Com tolerância de 5 dias o par fecharia.",  # dias inventados
        "Valor de R$79,6.",  # valor em formato diferente do dos fatos
    ],
)
def test_rejeita_numero_que_nao_esta_nos_fatos(texto):
    assert not texto_respeita_fatos(texto, _par_de_data())


def test_valida_contra_todos_os_candidatos():
    contexto = _divergente_valor(4)
    assert texto_respeita_fatos("O mais próximo é de -R$ 1.499,00.", contexto)
    assert texto_respeita_fatos("Diferença de R$ 1,00.", contexto)
    assert not texto_respeita_fatos("Diferença de R$ 2,00.", contexto)


# filtro por frase


def test_filtro_remove_so_as_frases_com_numero_inventado():
    texto = (
        "No banco há -R$ 79,61 em 01/10/2026. O sistema lançou em 03/10/2026. "
        "A diferença é de R$ 80,00! O sistema lançou 2 dias depois? "
        "A tolerância era de 0 dias. Justifique a divergência."
    )
    assert filtrar_texto_pelos_fatos(texto, _par_de_data()) == (
        "No banco há -R$ 79,61 em 01/10/2026. A tolerância era de 0 dias. Justifique a divergência."
    )


def test_filtro_sem_nenhuma_frase_valida_devolve_none():
    texto = "O sistema lançou em 03/10/2026. Diferença de R$ 80,00. Foram 9 dias."
    assert filtrar_texto_pelos_fatos(texto, _par_de_data()) is None
    assert filtrar_texto_pelos_fatos("   ", _par_de_data()) is None


def test_filtro_mantem_texto_todo_valido_igual():
    texto = texto_deterministico(_divergente_valor(4))
    assert filtrar_texto_pelos_fatos(texto, _divergente_valor(4)) == texto


def test_divisao_de_frases_nao_quebra_milhar_nem_data():
    texto = "No banco: -R$ 1.500,00 em 20/09/2026. No sistema: -R$ 1.499,00 em 20/09/2026."
    assert dividir_frases(texto) == [
        "No banco: -R$ 1.500,00 em 20/09/2026.",
        "No sistema: -R$ 1.499,00 em 20/09/2026.",
    ]
    assert filtrar_texto_pelos_fatos(texto, _divergente_valor(4)) == texto
