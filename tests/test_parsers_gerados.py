"""Reforço do critério "arquivo corrompido" da issue #11, usando o gerador
sintético da issue #10 em vez de bytes de lixo hardcoded.

tests/test_parser_ofx.py e tests/test_parser_csv.py já cobrem os 4 critérios
da #11 com fixtures manuais (#8/#9). Este arquivo não substitui aquela
cobertura — complementa confirmando que os parsers também levantam
`OFXInvalidoError`/`CSVInvalidoError` pra corrupção "realista" (header OFX
sem `OFXHEADER:100`, campo CSV com aspas nunca fechadas), que é o que
`scripts/gerar_extratos_sinteticos.py --corromper` reproduz de verdade a
partir dos erros que ofxtools/pandas levantam (ver docstrings de
`_corromper_ofx`/`_corromper_csv` nesse script).

Chama a rotina de geração como função Python — `_gerar_conteudo`, importada
direto do módulo do script, sem subprocess — com seed fixo, pra manter o
teste determinístico.

Critério da issue #11 coberto por este arquivo: arquivo corrompido.
"""

import random

import pytest

from app.parsers.csv import CSVInvalidoError, parse_csv
from app.parsers.ofx import OFXInvalidoError, parse_ofx
from scripts.gerar_extratos_sinteticos import _gerar_conteudo

# Seed fixo só pra este teste ser determinístico entre execuções — não tem
# relação com o seed usado em nenhuma fixture manual das #8/#9.
_SEED = 11


def test_ofx_corrompido_gerado_pelo_script_levanta_ofx_invalido_error():
    conteudo = _gerar_conteudo(
        formato="ofx",
        quantidade=5,
        encoding="utf-8",
        corromper=True,
        rng=random.Random(_SEED),
    )

    with pytest.raises(OFXInvalidoError):
        parse_ofx(conteudo)


def test_csv_corrompido_gerado_pelo_script_levanta_csv_invalido_error():
    conteudo = _gerar_conteudo(
        formato="csv",
        quantidade=5,
        encoding="utf-8",
        corromper=True,
        rng=random.Random(_SEED),
    )

    with pytest.raises(CSVInvalidoError):
        parse_csv(conteudo)
