"""Marcadores da conversa com o assistente (issue #223, ADR-028).

Nenhum dado sensível vai à IA. Antes de a pergunta sair para o provedor, o
backend troca cada valor por `[VALOR_n]` e cada data por `[DATA_n]`, além da
máscara de identificadores do ADR-011 (e-mail, CNPJ, CPF, telefone e
sequências longas de dígitos). O mapa entre marcador e valor fica só aqui, na
memória da requisição; o modelo devolve o marcador, e o backend o troca de
volta.

Ordem, em cada turno da pessoa e na pergunta:

1. identificadores (`mascarar_identificadores`);
2. datas completas (`12/10/2026`, `12/10/26`, `2026-10-12`);
3. mês com ano (`08/2026`, `2026-08`, `setembro de 2026`, `set/2026`) e ano
   depois de "em", "ano", "desde" ou "até" ficam no texto: não são dado
   sensível, e o modelo precisa deles para achar a competência;
4. sequência longa de dígitos com hífen ou espaço (conta, agência com
   dígito) vira `[NUMERO]`, antes de algum pedaço dela parecer valor;
5. datas sem ano (`3 de outubro`, `12/10`, `dia 12`);
6. valores: com `R$`, com milhar (`3.200`), com centavos (`1234,56`,
   `1234.56`) e qualquer inteiro de 3 ou mais dígitos que sobrou;
7. o último passo da máscara (`mascarar_numeros_longos`), por garantia.

O mesmo valor ou a mesma data reusa o marcador, e a numeração é única na
conversa: o modelo pode apontar um valor de uma pergunta anterior.

Nos turnos do assistente (respostas anteriores, que podem ser de dados), todo
número vira `[N]`, depois da máscara de identificadores. "E em outubro?"
continua funcionando, porque o nome do mês não é número.

Os marcadores são montados com caracteres de uso privado do Unicode durante o
processamento, para nenhum passo seguinte ler o número de um marcador.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services.ia.base import MensagemHistorico
from app.services.ia.mascaramento import mascarar_identificadores, mascarar_numeros_longos

_MESES = {
    "janeiro": 1,
    "jan": 1,
    "fevereiro": 2,
    "fev": 2,
    "março": 3,
    "marco": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "maio": 5,
    "mai": 5,
    "junho": 6,
    "jun": 6,
    "julho": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "setembro": 9,
    "set": 9,
    "outubro": 10,
    "out": 10,
    "novembro": 11,
    "nov": 11,
    "dezembro": 12,
    "dez": 12,
}
_MES = "|".join(sorted(_MESES, key=len, reverse=True))

_DATA_BR_COMPLETA = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])")
_DATA_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_COMPETENCIA = re.compile(
    rf"(?<![\d/])(?:0?[1-9]|1[0-2])/\d{{4}}(?![\d/])"
    rf"|(?<![\d-])\d{{4}}-(?:0[1-9]|1[0-2])(?![\d-])"
    rf"|\b(?:{_MES})\.?\s*(?:de\s+|/\s*)?\d{{4}}(?!\d)"
    rf"|\b(?:em|ano|desde|até|ate)\s+20\d{{2}}(?!\d)",
    re.IGNORECASE,
)
_SEQUENCIA_COM_HIFEN_OU_ESPACO = re.compile(r"\d+(?:[\-\s]\d+)+")
_DIA_DE_MES = re.compile(rf"(?<!\d)(\d{{1,2}})\s+de\s+({_MES})\b", re.IGNORECASE)
_DIA_BARRA_MES = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_DIA_SOLTO = re.compile(r"\b(dia)\s+(\d{1,2})(?!\d)", re.IGNORECASE)
_VALOR = re.compile(
    r"R\$\s*-?\s*\d[\d.]*(?:,\d{1,2})?(?![\d,])"
    r"|(?<![\d,.])-?\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?(?![\d,.])"
    r"|(?<![\d,.])-?\d+,\d{1,2}(?![\d,.])"
    r"|(?<![\d,.])-?\d+\.\d{2}(?![\d,.])"
    r"|(?<![\d,.])\d{3,}(?![\d,.])"
)
_DIGITOS = re.compile(r"\d+(?:[.,/\-]\d+)*")

_PRIMEIRO_RESERVADO = 0xE000


@dataclass(frozen=True)
class DataMarcada:
    dia: int
    mes: int | None
    ano: int | None


@dataclass(frozen=True)
class ConversaMarcada:
    pergunta: str
    historico: tuple[MensagemHistorico, ...]
    valores: dict[str, Decimal]
    datas: dict[str, DataMarcada]


def _ano(texto: str) -> int:
    ano = int(texto)
    return 2000 + ano if len(texto) == 2 else ano


def _data_valida(dia: int, mes: int | None) -> bool:
    return 1 <= dia <= 31 and (mes is None or 1 <= mes <= 12)


def _valor(texto: str) -> Decimal | None:
    limpo = texto.replace("R$", "").replace(" ", "")
    if "," in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", limpo):
        limpo = limpo.replace(".", "")
    try:
        return Decimal(limpo)
    except InvalidOperation:
        return None


class _Marcador:
    """O estado de uma conversa: os marcadores já dados e os trechos
    protegidos (competências), cada um num caractere reservado."""

    def __init__(self) -> None:
        self.valores: dict[str, Decimal] = {}
        self.datas: dict[str, DataMarcada] = {}
        self._reservados: list[str] = []

    def _reservar(self, texto_final: str) -> str:
        self._reservados.append(texto_final)
        return chr(_PRIMEIRO_RESERVADO + len(self._reservados) - 1)

    def _marcador(self, prefixo: str, mapa: dict, chave) -> str:
        for rotulo, existente in mapa.items():
            if existente == chave:
                return self._reservar(rotulo)
        rotulo = f"[{prefixo}_{len(mapa) + 1}]"
        mapa[rotulo] = chave
        return self._reservar(rotulo)

    def _data(self, dia: int, mes: int | None, ano: int | None, original: str) -> str:
        if not _data_valida(dia, mes):
            return original
        return self._marcador("DATA", self.datas, DataMarcada(dia, mes, ano))

    def _restaurar(self, texto: str) -> str:
        for indice, final in enumerate(self._reservados):
            texto = texto.replace(chr(_PRIMEIRO_RESERVADO + indice), final)
        return texto

    def marcar(self, texto: str) -> str:
        texto = mascarar_identificadores(texto)
        texto = _DATA_BR_COMPLETA.sub(
            lambda m: self._data(int(m[1]), int(m[2]), _ano(m[3]), m[0]), texto
        )
        texto = _DATA_ISO.sub(lambda m: self._data(int(m[3]), int(m[2]), int(m[1]), m[0]), texto)
        texto = _COMPETENCIA.sub(lambda m: self._reservar(m[0]), texto)
        texto = _SEQUENCIA_COM_HIFEN_OU_ESPACO.sub(lambda m: mascarar_numeros_longos(m[0]), texto)
        texto = _DIA_DE_MES.sub(
            lambda m: self._data(int(m[1]), _MESES[m[2].lower()], None, m[0]), texto
        )
        texto = _DIA_BARRA_MES.sub(lambda m: self._data(int(m[1]), int(m[2]), None, m[0]), texto)
        texto = _DIA_SOLTO.sub(lambda m: f"{m[1]} {self._data(int(m[2]), None, None, m[2])}", texto)
        texto = _VALOR.sub(self._substituir_valor, texto)
        return self._restaurar(mascarar_numeros_longos(texto))

    def _substituir_valor(self, encontrado: re.Match[str]) -> str:
        valor = _valor(encontrado[0])
        if valor is None:
            return encontrado[0]
        return self._marcador("VALOR", self.valores, valor)


def _sem_numeros(texto: str) -> str:
    return _DIGITOS.sub("[N]", mascarar_identificadores(texto))


MARCADOR_DE_EMPRESA = "[EMPRESA]"
# Nome mais curto que isso não é trocado: pegaria palavras comuns.
MINIMO_DE_LETRAS_DO_NOME = 3


def _trocar_nomes(texto: str, nomes: Iterable[str]) -> str:
    """Cada nome de `nomes` em `texto`, sem diferença de maiúscula e só como
    palavra inteira, vira `[EMPRESA]`. O mais longo primeiro, para "Alfa
    Comércio Ltda" não sobrar como "[EMPRESA] Comércio Ltda"."""
    for nome in sorted({n.strip() for n in nomes}, key=len, reverse=True):
        if len(nome) < MINIMO_DE_LETRAS_DO_NOME:
            continue
        padrao = re.compile(rf"(?<!\w){re.escape(nome)}(?!\w)", re.IGNORECASE)
        texto = padrao.sub(MARCADOR_DE_EMPRESA, texto)
    return texto


def marcar_conversa(
    pergunta: str, historico: Iterable[tuple[str, str]], nomes: Iterable[str] = ()
) -> ConversaMarcada:
    """A pergunta e o histórico (pares papel e texto, na ordem) prontos para o
    provedor, com o mapa de marcadores. `nomes` são as razões sociais da conta:
    a resposta sobre as empresas (`empresas_do_mes`) volta no histórico com
    elas, e a pessoa pode digitá-las na pergunta; nenhuma vai ao provedor."""
    nomes = tuple(nomes)
    marcador = _Marcador()
    turnos = tuple(
        MensagemHistorico(
            papel=papel,
            texto=(
                marcador.marcar(_trocar_nomes(texto, nomes))
                if papel == "usuario"
                else _sem_numeros(_trocar_nomes(texto, nomes))
            ),
        )
        for papel, texto in historico
    )
    return ConversaMarcada(
        pergunta=marcador.marcar(_trocar_nomes(pergunta, nomes)),
        historico=turnos,
        valores=marcador.valores,
        datas=marcador.datas,
    )
