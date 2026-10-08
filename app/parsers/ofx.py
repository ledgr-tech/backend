"""Parser de extratos OFX via ofxtools (issue #8).

Converte o conteúdo bruto de um arquivo OFX (bytes) num `ResultadoParsing`
(`app.parsers.tipos`) — lançamentos válidos + erros por transação — sem
tocar o banco. Formato suportado conforme ADR-001
(02-decisoes/01-formatos-suportados-mvp.md).

Por decisão confirmada com o time, esta issue cobre só a função de parsing.
Persistir lançamento (model `Lancamento` + migration com `empresa_id`
indexado e a constraint UNIQUE(empresa_id, extrato_id, hash_dedup) já
especificada na ADR-004) foi implementado na issue #12
(app/services/normalizacao.py), que chama esta função a partir de uma
BackgroundTask agendada pelo endpoint de upload.

Erro estrutural do arquivo inteiro (header/SGML malformado, estrutura fora
da spec OFX) continua abortando com `OFXInvalidoError` — a validação do
ofxtools acontece pra todo o documento em `tree.convert()`, antes de
qualquer transação individual ser acessada, então esse tipo de erro nunca
chega a ser "por transação". Já uma transação que falha só na hora de
*nós* construirmos o `LancamentoNormalizado` a partir dela (issue #13) não
aborta mais o arquivo: vira um `ErroLinha` no resultado, e o parsing segue
pras próximas transações.
"""

import io
from decimal import Decimal

from ofxtools.Parser import OFXTree

from app.parsers.tipos import ErroLinha, LancamentoNormalizado, ResultadoParsing

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


def parse_ofx(conteudo: bytes) -> ResultadoParsing:
    """Faz o parsing de um arquivo OFX e retorna lançamentos válidos + erros por transação.

    Levanta `OFXInvalidoError` se `conteudo` não for um OFX válido — erro
    estrutural do arquivo inteiro. Uma transação que falha só na
    construção do `LancamentoNormalizado` não levanta: vira um `ErroLinha`
    no `ResultadoParsing` (issue #13) e o parsing continua pras próximas.
    """
    tree = OFXTree()
    try:
        tree.parse(io.BytesIO(conteudo))
        ofx = tree.convert()
    except _ERROS_OFX_INVALIDO as exc:
        raise OFXInvalidoError(f"Arquivo OFX inválido: {exc}") from exc

    lancamentos: list[LancamentoNormalizado] = []
    erros: list[ErroLinha] = []
    posicao = 0
    for extrato in ofx.statements:
        for transacao in extrato.transactions:
            posicao += 1
            try:
                valor = transacao.trnamt
                lancamentos.append(
                    LancamentoNormalizado(
                        data=transacao.dtposted.date(),
                        valor=valor,
                        descricao=transacao.memo or transacao.name or "",
                        tipo=_tipo_normalizado(transacao.trntype, valor),
                    )
                )
            except _ERROS_OFX_INVALIDO as exc:
                fitid = getattr(transacao, "fitid", None)
                identificador = fitid if fitid else f"transação {posicao}"
                erros.append(ErroLinha(identificador=identificador, motivo=str(exc)))
    return ResultadoParsing(lancamentos=lancamentos, erros=erros)
