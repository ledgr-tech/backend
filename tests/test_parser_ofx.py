from datetime import date
from decimal import Decimal
from pathlib import Path

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


def test_parse_ofx_valido_retorna_lancamentos_normalizados():
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    lancamentos = parse_ofx(conteudo)

    assert lancamentos == [
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
