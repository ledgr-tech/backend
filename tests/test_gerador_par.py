"""Modo --par do gerador sintético (issue #16): sobreposição controlada."""

import random

import pytest

from scripts.gerar_extratos_sinteticos import _gerar_par, main


def _chaves(lancamentos):
    return [(item.valor, item.data) for item in lancamentos]


def test_gerar_par_tem_sobreposicao_exata_e_extras_sem_colisao():
    banco, sistema = _gerar_par(60, 35, random.Random(3))

    assert len(banco) == len(sistema) == 60
    identicos = {(i.valor, i.data, i.descricao) for i in banco} & {
        (i.valor, i.data, i.descricao) for i in sistema
    }
    comuns = set(_chaves(banco)) & set(_chaves(sistema))
    assert len(comuns) <= 35
    # tudo que colide em (valor, data) entre os lados é lançamento sobreposto idêntico
    assert {(v, d) for v, d, _ in identicos} == comuns


def test_gerar_par_e_deterministico_pela_seed():
    primeiro = _gerar_par(30, 10, random.Random(9))
    segundo = _gerar_par(30, 10, random.Random(9))

    assert [(i.valor, i.data, i.descricao) for i in primeiro[0]] == [
        (i.valor, i.data, i.descricao) for i in segundo[0]
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["--par"],
        ["--par", "--sobreposicao", "99", "--quantidade", "10"],
        ["--sobreposicao", "3"],
        ["--par", "--sobreposicao", "1", "--corromper"],
    ],
)
def test_argumentos_invalidos_do_modo_par_sao_rejeitados(args, tmp_path):
    with pytest.raises(SystemExit):
        main(["--formato", "csv", "--saida", str(tmp_path), *args])
