"""Lógica pura da exportação CSV do relatório de conciliação (issue #26).

Fica separada de app/api/conciliacoes.py de propósito: o endpoint só valida,
consulta e chama isto, pra formatação/neutralização de fórmula serem
testáveis sem banco nem HTTP.

Formato Excel BR (issue #26): separador `;`, UTF-8 com BOM, CRLF, aspas só
quando necessário (`csv.QUOTE_MINIMAL`). Data `dd/mm/aaaa`, valor e score com
vírgula decimal (sem separador de milhar), campos nulos saem vazios.

Neutralização de injeção de fórmula (`neutralizar_formula`): só nas colunas
de descrição — nunca em valor, data, status, regra ou score, porque um
débito como -150,00 não pode ser alterado. Segue a mitigação usual de CSV
injection (OWASP): célula cujo primeiro caractere não-espaço é um dos
gatilhos que o Excel/Sheets interpretam como início de fórmula ou comando
(`=`, `+`, `-`, `@`, tab, CR) ganha um apóstrofo no início, forçando o
consumidor a tratar a célula como texto.

Nunca loga descrição, valor nem nome de arquivo (mesma regra de
app/services/matching.py).
"""

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

CABECALHO_EXPORTACAO = [
    "Status",
    "Data (banco)",
    "Valor (banco)",
    "Descrição (banco)",
    "Data (sistema)",
    "Valor (sistema)",
    "Descrição (sistema)",
    "Regra aplicada",
    "Score de confiança",
]

# Rótulo em português de cada status de conciliacoes (ADR-007/ADR-009, ver
# app/services/matching.py) — mapa único, pra não duplicar tradução em outro
# lugar do backend.
ROTULOS_STATUS = {
    "match_exato": "Match exato",
    "match_tolerancia": "Match por tolerância de data",
    "duplicado": "Duplicado",
    "sem_correspondencia": "Sem correspondência",
    "tarifa_bancaria": "Tarifa bancária",
    "divergente_valor": "Divergência de valor",
    "divergente_data": "Divergência de data",
}

ROTULOS_REGRA = {
    "exato": "Exato",
    "tolerancia": "Tolerância de data",
    "fuzzy_descricao": "Similaridade de descrição",
}

# Caracteres que, como primeiro caractere não-espaço de uma célula, o
# Excel/Sheets podem interpretar como início de fórmula ou comando.
_GATILHOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class LinhaExportacao:
    """Um item do relatório (par, sobra de um lado, ou divergência) pronto
    pra virar uma linha do CSV — o mesmo recorte de `ItemConciliacaoResponse`
    (app/api/conciliacoes.py), sem o `id`/`extrato_sistema_id` que não
    entram na exportação."""

    status: str
    regra_aplicada: str | None
    score_confianca: Decimal | None
    banco_data: date | None
    banco_valor: Decimal | None
    banco_descricao: str | None
    sistema_data: date | None
    sistema_valor: Decimal | None
    sistema_descricao: str | None


def neutralizar_formula(texto: str) -> str:
    """Prefixa `texto` com um apóstrofo se o primeiro caractere não-espaço
    for um gatilho de fórmula (ver docstring do módulo). Espaços à esquerda
    contam pra achar o primeiro caractere, mas o apóstrofo vai no início
    absoluto da string, não depois deles. Texto normal e string vazia saem
    intactos."""
    sem_espacos_a_esquerda = texto.lstrip(" ")
    if sem_espacos_a_esquerda and sem_espacos_a_esquerda[0] in _GATILHOS_FORMULA:
        return "'" + texto
    return texto


def formatar_data(data: date | None) -> str:
    """`dd/mm/aaaa`, vazio quando `data` é `None`."""
    if data is None:
        return ""
    return data.strftime("%d/%m/%Y")


def formatar_valor(valor: Decimal | None) -> str:
    """Vírgula decimal, duas casas, com sinal, sem separador de milhar
    (ex: "1500,00", "-150,00"). Vazio quando `valor` é `None`."""
    if valor is None:
        return ""
    return f"{valor:.2f}".replace(".", ",")


def formatar_score(score: Decimal | None) -> str:
    """Vírgula decimal, três casas (ex: "1,000", "0,500"). Vazio quando
    `score` é `None`."""
    if score is None:
        return ""
    return f"{score:.3f}".replace(".", ",")


def _celula_descricao(descricao: str | None) -> str:
    if descricao is None:
        return ""
    return neutralizar_formula(descricao)


def gerar_csv_conciliacoes(linhas: list[LinhaExportacao]) -> bytes:
    """Monta o CSV inteiro em memória (`io.StringIO`) e devolve os bytes já
    com o BOM UTF-8 (`EF BB BF`) no início — quem chama devolve isso direto
    como corpo da resposta, sem `StreamingResponse` (issue #26: o volume de
    linhas de uma conciliação não justifica streaming).

    `csv.writer` com `delimiter=";"`, `lineterminator="\\r\\n"` e
    `QUOTE_MINIMAL` cuida de aspas e quebras de linha dentro da descrição —
    o módulo `csv` escapa e envolve em aspas sozinho quando necessário, sem
    ajuda daqui.
    """
    buffer = io.StringIO()
    escritor = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    escritor.writerow(CABECALHO_EXPORTACAO)
    for linha in linhas:
        escritor.writerow(
            [
                ROTULOS_STATUS.get(linha.status, linha.status),
                formatar_data(linha.banco_data),
                formatar_valor(linha.banco_valor),
                _celula_descricao(linha.banco_descricao),
                formatar_data(linha.sistema_data),
                formatar_valor(linha.sistema_valor),
                _celula_descricao(linha.sistema_descricao),
                ROTULOS_REGRA.get(linha.regra_aplicada, "") if linha.regra_aplicada else "",
                formatar_score(linha.score_confianca),
            ]
        )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
