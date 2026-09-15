from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.parsers.ofx import OFXInvalidoError, parse_ofx
from app.parsers.tipos import LancamentoNormalizado

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Fixture criada à mão pra issue #8 (não existe ainda o script gerador de
# extratos sintéticos da issue #10) — ver tests/fixtures/extrato_valido.ofx.
#
# Cobertura dos critérios da issue #11 ("Testes unitários de parsing")
# neste arquivo:
# - OFX válido: test_parse_ofx_valido_retorna_lancamentos_normalizados
# - Arquivo corrompido: test_parse_ofx_corrompido_levanta_erro_especifico
#   (conteúdo genérico inválido) e test_parse_ofx_vazio_levanta_erro_especifico
#   (caso extremo de "corrompido": arquivo vazio)
# Reforço com corrupção "realista" (gerada pelo script da #10, não bytes de
# lixo hardcoded): ver tests/test_parsers_gerados.py.
#
# Cobertura da tolerância por transação da issue #13 (ver docstring do
# módulo app/parsers/ofx.py): test_parse_ofx_transacao_malformada_com_fitid_*
# e test_parse_ofx_transacao_malformada_sem_fitid_*.


def test_parse_ofx_valido_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    resultado = parse_ofx(conteudo)

    assert resultado.erros == []
    assert resultado.lancamentos == [
        LancamentoNormalizado(
            data=date(2026, 9, 5),
            valor=Decimal("1500.00"),
            descricao="Deposito cliente XYZ",
            tipo="credito",
        ),
        LancamentoNormalizado(
            data=date(2026, 9, 6),
            valor=Decimal("-250.75"),
            descricao="Pagamento fornecedor ABC",
            tipo="debito",
        ),
    ]


def test_parse_ofx_corrompido_levanta_erro_especifico():
    with pytest.raises(OFXInvalidoError):
        parse_ofx(b"isso claramente nao eh um arquivo ofx")


def test_parse_ofx_vazio_levanta_erro_especifico():
    with pytest.raises(OFXInvalidoError):
        parse_ofx(b"")


# As duas próximas simulam o objeto já convertido pelo ofxtools (via mock de
# OFXTree), em vez de um arquivo OFX bruto real: o ofxtools valida os campos
# obrigatórios (TRNAMT, DTPOSTED) na conversão da estrutura inteira e aborta
# o arquivo todo antes do laço por transação — testado empiricamente com
# vários OFX malformados de verdade, sempre abortando o arquivo inteiro (ver
# discussão da issue #13). Não existe forma de produzir, com um arquivo OFX
# real, uma transação que passe pela validação estrutural do ofxtools e
# ainda assim falhe só na nossa construção do LancamentoNormalizado — então
# o teste exercita esse ponto exato do código diretamente.


def _transacao(**kwargs):
    base = {
        "fitid": None,
        "dtposted": None,
        "trnamt": None,
        "memo": None,
        "name": None,
        "trntype": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_parse_ofx_transacao_malformada_com_fitid_tolera_e_usa_fitid_como_identificador():
    transacao_ok = _transacao(
        fitid="FIT-1",
        dtposted=datetime(2026, 9, 5, tzinfo=timezone.utc),
        trnamt=Decimal("100.00"),
        trntype="CREDIT",
    )
    transacao_malformada = _transacao(
        fitid="FIT-BROKEN", dtposted=None, trnamt=Decimal("50.00"), trntype="CREDIT"
    )
    ofx_fake = SimpleNamespace(
        statements=[SimpleNamespace(transactions=[transacao_ok, transacao_malformada])]
    )

    with patch("app.parsers.ofx.OFXTree") as MockOFXTree:
        MockOFXTree.return_value.convert.return_value = ofx_fake
        resultado = parse_ofx(b"conteudo irrelevante, ofxtools mockado")

    assert len(resultado.lancamentos) == 1
    assert resultado.lancamentos[0].valor == Decimal("100.00")
    assert len(resultado.erros) == 1
    assert resultado.erros[0].identificador == "FIT-BROKEN"


def test_parse_ofx_transacao_malformada_sem_fitid_usa_posicao_sequencial_como_identificador():
    transacao_ok = _transacao(
        fitid="FIT-1",
        dtposted=datetime(2026, 9, 5, tzinfo=timezone.utc),
        trnamt=Decimal("100.00"),
        trntype="CREDIT",
    )
    transacao_malformada = _transacao(
        fitid=None, dtposted=None, trnamt=Decimal("50.00"), trntype="CREDIT"
    )
    ofx_fake = SimpleNamespace(
        statements=[SimpleNamespace(transactions=[transacao_ok, transacao_malformada])]
    )

    with patch("app.parsers.ofx.OFXTree") as MockOFXTree:
        MockOFXTree.return_value.convert.return_value = ofx_fake
        resultado = parse_ofx(b"conteudo irrelevante, ofxtools mockado")

    assert len(resultado.lancamentos) == 1
    assert len(resultado.erros) == 1
    # 1-based, posição sequencial entre todas as transações do arquivo —
    # a malformada é a segunda transação processada.
    assert resultado.erros[0].identificador == "transação 2"
