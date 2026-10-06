"""As duas estratégias de extração do spike, com pdfplumber, devolvendo a mesma
lista de `Lancamento` (data, descrição, valor com sinal).

- "tabela": `extract_tables` (grade de linhas; sem grade, cai para a
  detecção por alinhamento de texto do pdfplumber).
- "texto": palavras com posição (`extract_words`) agrupadas em linhas. Uma
  linha de lançamento começa com data (DD/MM/AAAA ou DD/MM/AA). O cabeçalho dá
  a posição x das colunas, para separar valor, entrada/saída, saldo e
  documento. Descrição que quebra na linha seguinte é juntada à anterior.

Nas duas: cabeçalho repetido, rodapé, "saldo anterior", "saldo", "subtotal",
"total" e linhas sem data são ignorados, e a coluna de saldo nunca vira valor.
PDF sem camada de texto (todas as páginas sem texto) levanta `PdfSemTexto`.

Funciona sem configuração. Um layout pode ter um `<nome>.layout.toml` ao lado
do PDF com sinônimos de cabeçalho e padrões de linhas a ignorar (ver
layouts/exemplo.toml); o relatório diz quando a configuração foi usada.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import tomllib

from scripts.spike_pdf.modelo import Lancamento
from scripts.spike_pdf.pareamento import normalizar_descricao
from scripts.spike_pdf.valores import parece_valor, parse_data_br, parse_valor_br

ESTRATEGIAS = ("tabela", "texto")

SINONIMOS_PADRAO = {
    "data": ["data", "dt", "dt. lanc", "data lanc"],
    "descricao": ["historico", "descricao", "complemento", "hist"],
    "documento": ["documento", "doc", "doc.", "nf", "numero", "n."],
    "valor": ["valor", "vlr", "vlr.", "montante"],
    "entrada": ["entrada", "entradas", "credito", "creditos"],
    "saida": ["saida", "saidas", "debito", "debitos"],
    "saldo": ["saldo"],
    "dc": ["d/c", "c/d", "dc", "cd"],
}
COLUNAS_DE_VALOR = ("valor", "entrada", "saida", "saldo")
IGNORAR_PADRAO = [
    r"^saldo\b",
    r"^sub-?total\b",
    r"^totai?s?\b",
    r"^(a )?transportar\b",
    r"^transporte\b",
    r"^pagina \d+",
]


class PdfSemTexto(Exception):
    """PDF sem camada de texto (escaneado): fora do escopo do spike."""


@dataclass
class Layout:
    sinonimos: dict[str, list[str]]
    ignorar: list[re.Pattern]
    especifico: bool = False
    nome: str | None = None


def carregar_layout(pdf: Path, arquivo: Path | None = None) -> Layout:
    """Configuração do layout: `arquivo`, ou `<pdf>.layout.toml` ao lado do
    PDF, ou a padrão."""
    sinonimos = {chave: list(valores) for chave, valores in SINONIMOS_PADRAO.items()}
    ignorar = list(IGNORAR_PADRAO)
    caminho = arquivo or pdf.with_name(pdf.stem + ".layout.toml")
    if not caminho.exists():
        return Layout(sinonimos, [re.compile(p) for p in ignorar])
    config = tomllib.loads(caminho.read_text(encoding="utf-8"))
    for chave, extras in config.get("colunas", {}).items():
        sinonimos.setdefault(chave, []).extend(normalizar_descricao(e) for e in extras)
    ignorar.extend(config.get("ignorar", []))
    return Layout(sinonimos, [re.compile(p) for p in ignorar], True, caminho.name)


@dataclass
class Extracao:
    lancamentos: list[Lancamento]
    paginas: int
    configuracao: str | None  # nome do arquivo de layout usado, ou None
    avisos: list[str] = field(default_factory=list)


def _ignorar(texto: str, layout: Layout) -> bool:
    normal = normalizar_descricao(texto)
    return any(padrao.search(normal) for padrao in layout.ignorar)


def _coluna_do_cabecalho(texto: str, layout: Layout) -> str | None:
    normal = normalizar_descricao(texto)
    for coluna, nomes in layout.sinonimos.items():
        if normal in nomes:
            return coluna
    return None


def _eh_cabecalho(colunas: set[str]) -> bool:
    return "data" in colunas and bool(colunas & {"valor", "entrada", "saida"})


def _valor_por_colunas(valores: dict[str, str], dc: str | None):
    """Valor com sinal a partir das células/palavras já atribuídas às colunas.
    Saldo nunca entra."""
    if valores.get("valor"):
        valor = parse_valor_br(valores["valor"])
    elif valores.get("entrada"):
        valor = parse_valor_br(valores["entrada"])
        valor = abs(valor) if valor is not None else None
    elif valores.get("saida"):
        valor = parse_valor_br(valores["saida"])
        valor = -abs(valor) if valor is not None else None
    else:
        return None
    if valor is not None and dc:
        letra = dc.strip().upper()[:1]
        if letra == "D":
            valor = -abs(valor)
        elif letra == "C":
            valor = abs(valor)
    return valor


def abrir(caminho: Path) -> pdfplumber.PDF:
    pdf = pdfplumber.open(caminho)
    if all(not (pagina.extract_text() or "").strip() for pagina in pdf.pages):
        pdf.close()
        raise PdfSemTexto("PDF sem texto (escaneado), fora do escopo")
    return pdf


# Estratégia "tabela"

CONFIG_TABELA_TEXTO = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def _limpar(celula) -> str:
    return " ".join((celula or "").split())


def extrair_tabela(caminho: Path, layout: Layout) -> Extracao:
    lancamentos: list[Lancamento] = []
    avisos: list[str] = []
    with abrir(caminho) as pdf:
        mapa: dict[str, int] | None = None
        for pagina in pdf.pages:
            tabelas = pagina.extract_tables()
            if not tabelas:
                tabelas = pagina.extract_tables(CONFIG_TABELA_TEXTO)
                if tabelas and "sem grade" not in " ".join(avisos):
                    avisos.append("sem grade: tabelas detectadas pelo alinhamento do texto")
            for tabela in tabelas:
                ultimo = None
                for linha in tabela:
                    celulas = [_limpar(celula) for celula in linha]
                    colunas = {
                        coluna: indice
                        for indice, celula in enumerate(celulas)
                        if (coluna := _coluna_do_cabecalho(celula, layout))
                    }
                    if _eh_cabecalho(set(colunas)):
                        mapa = colunas
                        ultimo = None
                        continue
                    lancamento = _linha_de_tabela(celulas, mapa, layout)
                    if lancamento is not None:
                        lancamentos.append(lancamento)
                        ultimo = len(lancamentos) - 1
                        continue
                    ultimo = _continuacao_de_tabela(celulas, mapa, layout, lancamentos, ultimo)
        return Extracao(lancamentos, len(pdf.pages), layout.nome, avisos)


def _linha_de_tabela(celulas, mapa, layout) -> Lancamento | None:
    if mapa and mapa["data"] < len(celulas):
        data = parse_data_br(celulas[mapa["data"]])
    else:
        data = next((d for c in celulas if (d := parse_data_br(c))), None)
    if data is None:
        return None
    if mapa:

        def celula(coluna):
            indice = mapa.get(coluna)
            return celulas[indice] if indice is not None and indice < len(celulas) else ""

        descricao = _descricao_de_tabela(celulas, mapa)
        valor = _valor_por_colunas(
            {coluna: celula(coluna) for coluna in COLUNAS_DE_VALOR}, celula("dc") or None
        )
    else:
        textos = [c for c in celulas if c and not parse_data_br(c) and not parece_valor(c)]
        descricao = max(textos, key=len, default="")
        valores = [c for c in celulas if parece_valor(c)]
        valor = parse_valor_br(valores[0]) if valores else None
    if valor is None or not descricao or _ignorar(descricao, layout):
        return None
    return Lancamento(data, descricao, valor)


def _descricao_de_tabela(celulas, mapa) -> str:
    """A célula da descrição e as seguintes até a próxima coluna mapeada: sem
    grade, o pdfplumber costuma partir uma descrição em várias células."""
    inicio = mapa.get("descricao")
    if inicio is None or inicio >= len(celulas):
        return ""
    fim = min((i for i in mapa.values() if i > inicio), default=len(celulas))
    return " ".join(c for c in celulas[inicio:fim] if c and not parece_valor(c))


def _continuacao_de_tabela(celulas, mapa, layout, lancamentos, ultimo):
    """Linha sem data e sem valor logo depois de um lançamento: resto da
    descrição. Qualquer outra coisa encerra a continuação."""
    if ultimo is None or any(parece_valor(c) for c in celulas):
        return None
    texto = _descricao_de_tabela(celulas, mapa) if mapa else " ".join(c for c in celulas if c)
    if not texto or _ignorar(texto, layout):
        return None
    anterior = lancamentos[ultimo]
    lancamentos[ultimo] = Lancamento(anterior.data, f"{anterior.descricao} {texto}", anterior.valor)
    return ultimo


# Estratégia "texto"


def _linhas_da_pagina(pagina, tolerancia: float = 3.0) -> list[list[dict]]:
    palavras = sorted(
        pagina.extract_words(x_tolerance=1.5, y_tolerance=tolerancia),
        key=lambda p: (round(p["top"]), p["x0"]),
    )
    linhas: list[list[dict]] = []
    for palavra in palavras:
        if linhas and abs(palavra["top"] - linhas[-1][0]["top"]) <= tolerancia:
            linhas[-1].append(palavra)
        else:
            linhas.append([palavra])
    return [sorted(linha, key=lambda p: p["x0"]) for linha in linhas]


def _juntar_valores(linha: list[dict]) -> list[dict]:
    """Junta "R$" e o sufixo C/D separados à palavra do valor."""
    juntas: list[dict] = []
    for palavra in linha:
        texto = palavra["text"]
        if juntas and juntas[-1]["text"].upper() == "R$" and parece_valor(texto):
            juntas[-1] = {**palavra, "x0": juntas[-1]["x0"]}
            continue
        if juntas and texto.upper() in ("C", "D") and parece_valor(juntas[-1]["text"]):
            anterior = juntas[-1]
            juntas[-1] = {**anterior, "text": f"{anterior['text']} {texto}", "x1": palavra["x1"]}
            continue
        juntas.append(dict(palavra))
    return juntas


def _cabecalho_de_texto(linha: list[dict], layout: Layout) -> dict[str, dict] | None:
    colunas: dict[str, dict] = {}
    for indice in range(len(linha)):
        for tamanho in (3, 2, 1):  # cabeçalhos de mais de uma palavra ("D/C", "Dt. lanc")
            pedaco = linha[indice : indice + tamanho]
            if len(pedaco) < tamanho:
                continue
            coluna = _coluna_do_cabecalho(" ".join(p["text"] for p in pedaco), layout)
            if coluna and coluna not in colunas:
                colunas[coluna] = {"x0": pedaco[0]["x0"], "x1": pedaco[-1]["x1"]}
                break
    return colunas if _eh_cabecalho(set(colunas)) else None


def _coluna_de_texto(palavra, cabecalho) -> str:
    """Texto alinhado à esquerda: a coluna de cabeçalho com maior x0 que não
    passa do início da palavra."""
    candidatas = [
        (posicao["x0"], coluna)
        for coluna, posicao in cabecalho.items()
        if coluna not in COLUNAS_DE_VALOR and posicao["x0"] <= palavra["x0"] + 3
    ]
    return max(candidatas)[1] if candidatas else "descricao"


def _coluna_de_valor(palavra, cabecalho) -> str | None:
    """Valor: a coluna de valor/entrada/saída/saldo mais próxima, pela borda
    direita (números costumam ser alinhados à direita) ou pelo centro."""
    centro = (palavra["x0"] + palavra["x1"]) / 2
    distancias = [
        (
            min(
                abs(palavra["x1"] - posicao["x1"]),
                abs(centro - (posicao["x0"] + posicao["x1"]) / 2),
            ),
            coluna,
        )
        for coluna, posicao in cabecalho.items()
        if coluna in COLUNAS_DE_VALOR
    ]
    return min(distancias)[1] if distancias else None


def extrair_texto(caminho: Path, layout: Layout) -> Extracao:
    lancamentos: list[Lancamento] = []
    with abrir(caminho) as pdf:
        cabecalho: dict[str, dict] | None = None
        for pagina in pdf.pages:
            ultimo = None
            fundo_ultimo = 0.0
            for linha in _linhas_da_pagina(pagina):
                linha = _juntar_valores(linha)
                novo = _cabecalho_de_texto(linha, layout)
                if novo:
                    cabecalho = novo
                    ultimo = None
                    continue
                data = parse_data_br(linha[0]["text"])
                topo = min(p["top"] for p in linha)
                fundo = max(p["bottom"] for p in linha)
                if data is not None:
                    lancamento = _linha_de_texto(data, linha[1:], cabecalho, layout)
                    if lancamento is None:
                        ultimo = None
                        continue
                    lancamentos.append(lancamento)
                    ultimo, fundo_ultimo = len(lancamentos) - 1, fundo
                    continue
                texto = " ".join(p["text"] for p in linha)
                altura = fundo - topo
                continua = (
                    ultimo is not None
                    and not any(parece_valor(p["text"]) for p in linha)
                    and not _ignorar(texto, layout)
                    and topo - fundo_ultimo <= altura
                    and (
                        cabecalho is None
                        or "descricao" not in cabecalho
                        or abs(linha[0]["x0"] - cabecalho["descricao"]["x0"]) <= 20
                    )
                )
                if continua:
                    anterior = lancamentos[ultimo]
                    lancamentos[ultimo] = Lancamento(
                        anterior.data, f"{anterior.descricao} {texto}", anterior.valor
                    )
                    fundo_ultimo = fundo
                else:
                    ultimo = None
        return Extracao(lancamentos, len(pdf.pages), layout.nome)


def _linha_de_texto(data, palavras, cabecalho, layout) -> Lancamento | None:
    if cabecalho:
        textos: dict[str, list[str]] = {}
        valores: dict[str, str] = {}
        for palavra in palavras:
            if parece_valor(palavra["text"]):
                coluna = _coluna_de_valor(palavra, cabecalho)
                if coluna and coluna not in valores:
                    valores[coluna] = palavra["text"]
            else:
                textos.setdefault(_coluna_de_texto(palavra, cabecalho), []).append(palavra["text"])
        descricao = " ".join(textos.get("descricao", []))
        valor = _valor_por_colunas(valores, " ".join(textos.get("dc", [])) or None)
    else:
        valores_linha = [p["text"] for p in palavras if parece_valor(p["text"])]
        descricao = " ".join(p["text"] for p in palavras if not parece_valor(p["text"]))
        # Sem cabeçalho: o primeiro valor é o lançamento; um segundo, à direita,
        # costuma ser o saldo.
        valor = parse_valor_br(valores_linha[0]) if valores_linha else None
    if valor is None or not descricao or _ignorar(descricao, layout):
        return None
    return Lancamento(data, descricao, valor)


def extrair(caminho: Path, estrategia: str, layout: Layout) -> Extracao:
    if estrategia == "tabela":
        return extrair_tabela(caminho, layout)
    if estrategia == "texto":
        return extrair_texto(caminho, layout)
    raise ValueError(f"estratégia desconhecida: {estrategia}")
