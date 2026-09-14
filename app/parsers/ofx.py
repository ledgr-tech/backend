"""Parser de extratos OFX via ofxtools (issue #8).

Converte o conteúdo bruto de um arquivo OFX (bytes) numa lista de
lançamentos normalizados em memória (data, valor, descrição, tipo
credito|debito) — sem tocar o banco. Formato suportado conforme ADR-001
(02-decisoes/01-formatos-suportados-mvp.md).

Por decisão confirmada com o time, esta issue cobre só a função de parsing.
Persistir lançamento (model `Lancamento` + migration com `empresa_id`
indexado e a constraint UNIQUE(empresa_id, extrato_id, hash_dedup) já
especificada na ADR-004) nasce junto com a normalização via BackgroundTasks
(Sprint 2, ver 07-tecnico/backlog-de-sprints-do-mvp.md), evitando
retrabalho.
"""

import io
from decimal import Decimal

from ofxtools.Parser import OFXTree

from app.parsers.tipos import LancamentoNormalizado

# TRNTYPE conforme a spec OFX (seção "Banking Transaction Types"). Não é uma
# lista exaustiva de todos os valores possíveis da spec, só dos tipos
# esperados em extratos bancários brasileiros no MVP.
_TRNTYPES_CREDITO = {"CREDIT", "DEP", "DIRECTDEP", "INT", "DIV", "REPEATPMT"}
_TRNTYPES_DEBITO = {
    "DEBIT",
    "ATM",
    "POS",
    "PAYMENT",
    "CASH",
    "CHECK",
    "FEE",
    "SRVCHG",
    "DIRECTDEBIT",
    "XFER",
}

# Exceções que o ofxtools levanta pra header malformado (SyntaxError), corpo
# SGML/XML quebrado (SyntaxError/ET.ParseError, que também é SyntaxError) e
# estrutura fora da spec OFX (ValueError, via OFXSpecError/OFXTypeError) —
# ver ofxtools.header.OFXHeaderError, ofxtools.Parser.ParseError e
# ofxtools.models.base.OFXAggregateError. KeyError/AttributeError/IndexError
# cobrem falhas de estrutura inesperada que escapam dessa hierarquia.
_ERROS_OFX_INVALIDO = (SyntaxError, ValueError, KeyError, AttributeError, IndexError)


class OFXInvalidoError(Exception):
    """Conteúdo informado não é um OFX válido.

    Levantado tanto por header/SGML/XML malformado quanto por estrutura
    fora da spec OFX. Usado pela issue #11 (teste de arquivo corrompido).
    """


def _tipo_normalizado(trntype: str | None, valor: Decimal) -> str:
    trntype_upper = (trntype or "").upper()
    if trntype_upper in _TRNTYPES_CREDITO:
        return "credito"
    if trntype_upper in _TRNTYPES_DEBITO:
        return "debito"
    # TRNTYPE fora do mapeamento acima (ex: "OTHER") — usa o sinal do valor
    # como fallback em vez de falhar o parsing por um tipo não previsto.
    return "credito" if valor >= 0 else "debito"


def parse_ofx(conteudo: bytes) -> list[LancamentoNormalizado]:
    """Faz o parsing de um arquivo OFX e retorna os lançamentos normalizados.

    Levanta `OFXInvalidoError` se `conteudo` não for um OFX válido.
    """
    tree = OFXTree()
    try:
        tree.parse(io.BytesIO(conteudo))
        ofx = tree.convert()
    except _ERROS_OFX_INVALIDO as exc:
        raise OFXInvalidoError(f"Arquivo OFX inválido: {exc}") from exc

    lancamentos: list[LancamentoNormalizado] = []
    for extrato in ofx.statements:
        for transacao in extrato.transactions:
            valor = transacao.trnamt
            lancamentos.append(
                LancamentoNormalizado(
                    data=transacao.dtposted.date(),
                    valor=valor,
                    descricao=transacao.memo or transacao.name or "",
                    tipo=_tipo_normalizado(transacao.trntype, valor),
                )
            )
    return lancamentos
