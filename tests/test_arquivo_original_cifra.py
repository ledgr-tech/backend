"""Cifra e nome do objeto do arquivo original do extrato (issue #139, ADR-023).

Sem banco e sem R2: só as funções puras de app/services/arquivo_original.py.
"""

import base64
import uuid

import pytest

from app.services.arquivo_original import (
    ArquivoOriginalCorrompido,
    chave_do_objeto,
    cifrar,
    decifrar,
    ler_chave_de_cifra,
    prefixo_da_conta,
)

CHAVE = bytes(range(32))
OBJETO = "contas/c/empresas/e/x"
CONTEUDO = b"OFXHEADER:100\n<NAME>JOAO DA SILVA"


def test_chave_do_objeto_e_prefixo_da_conta():
    c, e, x = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    assert chave_do_objeto(c, e, x) == f"contas/{c}/empresas/{e}/{x}"
    assert prefixo_da_conta(c) == f"contas/{c}/"
    assert chave_do_objeto(c, e, x).startswith(prefixo_da_conta(c))


def test_cifrar_e_decifrar_devolve_o_original():
    assert decifrar(CHAVE, OBJETO, cifrar(CHAVE, OBJETO, CONTEUDO)) == CONTEUDO


def test_cifrado_comeca_pela_versao_e_nao_contem_o_original():
    dados = cifrar(CHAVE, OBJETO, CONTEUDO)
    assert dados[0] == 1
    assert b"JOAO" not in dados
    assert len(dados) == 1 + 12 + len(CONTEUDO) + 16


def test_cada_cifra_usa_um_nonce_novo():
    assert cifrar(CHAVE, OBJETO, CONTEUDO) != cifrar(CHAVE, OBJETO, CONTEUDO)


@pytest.mark.parametrize(
    "estragar",
    [
        pytest.param(lambda d: (bytes(32 * [7]), OBJETO, d), id="outra-chave-de-cifra"),
        pytest.param(lambda d: (CHAVE, "contas/c/empresas/e/y", d), id="copiado-para-outro-objeto"),
        pytest.param(lambda d: (CHAVE, OBJETO, b"\x02" + d[1:]), id="versao-desconhecida"),
        pytest.param(lambda d: (CHAVE, OBJETO, d[:20]), id="truncado"),
        pytest.param(lambda d: (CHAVE, OBJETO, d[:-1] + bytes([d[-1] ^ 1])), id="um-bit-trocado"),
    ],
)
def test_dados_estragados_levantam_corrompido(estragar):
    with pytest.raises(ArquivoOriginalCorrompido):
        decifrar(*estragar(cifrar(CHAVE, OBJETO, CONTEUDO)))


def test_ler_chave_de_cifra():
    valida = base64.b64encode(CHAVE).decode()
    assert ler_chave_de_cifra(valida) == CHAVE
    assert ler_chave_de_cifra(f"  {valida}\n") == CHAVE
    assert ler_chave_de_cifra("") is None
    assert ler_chave_de_cifra(base64.b64encode(bytes(16)).decode()) is None
    assert ler_chave_de_cifra("não é base64") is None
