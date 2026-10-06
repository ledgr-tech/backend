"""Gera PDFs sintéticos de relatório de ERP, cada um com o gabarito ao lado,
para testar a ferramenta antes das amostras reais. Dados inventados, sem
nenhum dado real; os arquivos podem ser versionados.

    python -m scripts.spike_pdf.gerar_sinteticos [--destino scripts/spike_pdf/sinteticos]

- layout_a: Data | Histórico | Documento | Valor (com sinal) | Saldo, 3
  páginas, com grade, cabeçalho repetido, saldo anterior, subtotal por página
  e total no fim.
- layout_b: Data | Descrição | Entrada | Saída, sem grade, com descrições que
  quebram em duas linhas.
- layout_c: valor com sufixo C/D e data DD/MM/AA.
- escaneado: só imagem, sem camada de texto (gabarito só com o total).

Determinístico: mesma semente, mesmos PDFs (`invariant=1` no reportlab).
"""

import argparse
import io
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from scripts.spike_pdf.gabarito import escrever_gabarito
from scripts.spike_pdf.modelo import Lancamento

DESTINO_PADRAO = Path(__file__).parent / "sinteticos"
LARGURA, ALTURA = A4

CURTAS = [
    "Pagamento fornecedor Alfa",
    "Recebimento cliente Beta",
    "Tarifa de manutenção",
    "Compra de material de escritório",
    "Recebimento duplicata 1234",
    "Pagamento de energia elétrica",
    "Transferência entre contas",
    "Venda à vista loja centro",
    "Pagamento de aluguel",
    "Recebimento PIX cliente Gama",
]
LONGAS = [
    "Pagamento a fornecedor Distribuidora Ômega Comércio de Peças referente NF 4521",
    "Recebimento de cliente Construtora Horizonte Engenharia parcela 3 de 6",
    "Pagamento de folha complementar de setembro dos colaboradores do setor fiscal",
    "Recebimento de vendas com cartão de crédito da maquininha loja shopping norte",
]


def _br(valor: Decimal) -> str:
    texto = f"{abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return texto


def _lancamentos(semente: int, quantidade: int, longas: bool = False) -> list[Lancamento]:
    gerador = random.Random(semente)
    dia = date(2026, 9, 1)
    lancamentos = []
    for indice in range(quantidade):
        dia += timedelta(days=gerador.choice([0, 0, 1]))
        if longas and indice % 4 == 1:
            descricao = gerador.choice(LONGAS)
        else:
            descricao = gerador.choice(CURTAS)
        centavos = gerador.randint(500, 2_500_000)
        sinal = -1 if gerador.random() < 0.5 else 1
        lancamentos.append(Lancamento(dia, descricao, Decimal(centavos * sinal) / 100))
    return lancamentos


def _cabecalho_relatorio(pdf: canvas.Canvas, titulo: str, pagina: int, paginas: int) -> None:
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(40, ALTURA - 40, "Empresa Exemplo Ltda")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, ALTURA - 56, titulo)
    pdf.drawString(40, ALTURA - 70, "Período: 01/09/2026 a 30/09/2026")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(40, 30, "Emitido em 06/10/2026 pelo sistema de gestão")
    pdf.drawRightString(LARGURA - 40, 30, f"Página {pagina} de {paginas}")


def gerar_layout_a(destino: Path) -> None:
    lancamentos = _lancamentos(semente=1, quantidade=66)
    por_pagina = 22
    paginas = [lancamentos[i : i + por_pagina] for i in range(0, len(lancamentos), por_pagina)]
    colunas = [40, 100, 330, 410, 485, 555]  # bordas: data, histórico, doc, valor, saldo
    saldo = Decimal("10000.00")
    altura_linha = 16
    pdf = canvas.Canvas(str(destino / "layout_a.pdf"), pagesize=A4, invariant=1)
    total = Decimal(0)
    for numero, pagina in enumerate(paginas, start=1):
        _cabecalho_relatorio(pdf, "Relatório de lançamentos financeiros", numero, len(paginas))
        y = ALTURA - 100
        linhas = [("Data", "Histórico", "Documento", "Valor", "Saldo")]
        if numero == 1:
            linhas.append(("01/09/2026", "Saldo anterior", "", "", _br(saldo)))
        subtotal = Decimal(0)
        for indice, lancamento in enumerate(pagina):
            saldo += lancamento.valor
            subtotal += lancamento.valor
            valor = ("-" if lancamento.valor < 0 else "") + _br(lancamento.valor)
            linhas.append(
                (
                    lancamento.data.strftime("%d/%m/%Y"),
                    lancamento.descricao[:40],
                    f"{1000 + indice + numero * 100}",
                    valor,
                    ("-" if saldo < 0 else "") + _br(saldo),
                )
            )
        total += subtotal
        linhas.append(
            ("", "Subtotal da página", "", ("-" if subtotal < 0 else "") + _br(subtotal), "")
        )
        if numero == len(paginas):
            linhas.append(("", "Total do período", "", ("-" if total < 0 else "") + _br(total), ""))
        for linha in linhas:
            pdf.setFont("Helvetica-Bold" if linha[0] == "Data" else "Helvetica", 8)
            for borda in colunas:
                pdf.line(borda, y, borda, y - altura_linha)
            pdf.line(colunas[0], y, colunas[-1], y)
            pdf.line(colunas[0], y - altura_linha, colunas[-1], y - altura_linha)
            base = y - altura_linha + 5
            pdf.drawString(colunas[0] + 3, base, linha[0])
            pdf.drawString(colunas[1] + 3, base, linha[1])
            pdf.drawString(colunas[2] + 3, base, linha[2])
            pdf.drawRightString(colunas[4] - 3, base, linha[3])
            pdf.drawRightString(colunas[5] - 3, base, linha[4])
            y -= altura_linha
        pdf.showPage()
    pdf.save()
    escrever_gabarito(
        destino / "layout_a.gabarito.csv",
        [Lancamento(lanc.data, lanc.descricao[:40], lanc.valor) for lanc in lancamentos],
    )


def _quebrar(descricao: str, largura: int = 46) -> list[str]:
    if len(descricao) <= largura:
        return [descricao]
    corte = descricao.rfind(" ", 0, largura)
    return [descricao[:corte], descricao[corte + 1 :]]


def gerar_layout_b(destino: Path) -> None:
    lancamentos = _lancamentos(semente=2, quantidade=40, longas=True)
    pdf = canvas.Canvas(str(destino / "layout_b.pdf"), pagesize=A4, invariant=1)
    paginas = 2
    numero = 1

    def nova_pagina():
        _cabecalho_relatorio(pdf, "Movimento de caixa e bancos", numero, paginas)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(40, ALTURA - 100, "Data")
        pdf.drawString(110, ALTURA - 100, "Descrição")
        pdf.drawRightString(470, ALTURA - 100, "Entrada")
        pdf.drawRightString(555, ALTURA - 100, "Saída")
        pdf.line(40, ALTURA - 104, 555, ALTURA - 104)
        pdf.setFont("Helvetica", 9)
        return ALTURA - 120

    y = nova_pagina()
    metade = len(lancamentos) // 2
    for indice, lancamento in enumerate(lancamentos):
        if indice == metade:
            pdf.showPage()
            numero += 1
            y = nova_pagina()
        partes = _quebrar(lancamento.descricao)
        pdf.drawString(40, y, lancamento.data.strftime("%d/%m/%Y"))
        pdf.drawString(110, y, partes[0])
        coluna = 470 if lancamento.valor > 0 else 555
        pdf.drawRightString(coluna, y, _br(lancamento.valor))
        for parte in partes[1:]:
            y -= 11
            pdf.drawString(110, y, parte)
        y -= 15
    pdf.setFont("Helvetica-Bold", 9)
    entradas = sum(lanc.valor for lanc in lancamentos if lanc.valor > 0)
    saidas = sum(-lanc.valor for lanc in lancamentos if lanc.valor < 0)
    pdf.drawString(110, y - 5, "Totais")
    pdf.drawRightString(470, y - 5, _br(entradas))
    pdf.drawRightString(555, y - 5, _br(saidas))
    pdf.showPage()
    pdf.save()
    escrever_gabarito(destino / "layout_b.gabarito.csv", lancamentos)


def gerar_layout_c(destino: Path) -> None:
    lancamentos = _lancamentos(semente=3, quantidade=24)
    pdf = canvas.Canvas(str(destino / "layout_c.pdf"), pagesize=A4, invariant=1)
    _cabecalho_relatorio(pdf, "Extrato de lançamentos contábeis", 1, 1)
    pdf.setFont("Courier-Bold", 9)
    pdf.drawString(40, ALTURA - 100, "DATA      HISTÓRICO")
    pdf.drawRightString(540, ALTURA - 100, "VALOR")
    pdf.setFont("Courier", 9)
    y = ALTURA - 116
    pdf.drawString(110, y, "SALDO ANTERIOR")
    pdf.drawRightString(540, y, "5.000,00 C")
    for lancamento in lancamentos:
        y -= 13
        pdf.drawString(40, y, lancamento.data.strftime("%d/%m/%y"))
        pdf.drawString(110, y, lancamento.descricao.upper())
        sufixo = "D" if lancamento.valor < 0 else "C"
        pdf.drawRightString(540, y, f"{_br(lancamento.valor)} {sufixo}")
    y -= 18
    saldo = Decimal("5000.00") + sum(lanc.valor for lanc in lancamentos)
    pdf.drawString(110, y, "SALDO FINAL")
    pdf.drawRightString(540, y, f"{_br(saldo)} {'D' if saldo < 0 else 'C'}")
    pdf.showPage()
    pdf.save()
    escrever_gabarito(
        destino / "layout_c.gabarito.csv",
        [Lancamento(lanc.data, lanc.descricao.upper(), lanc.valor) for lanc in lancamentos],
    )


def gerar_escaneado(destino: Path) -> None:
    imagem = Image.new("L", (1240, 1754), color=255)
    desenho = ImageDraw.Draw(imagem)
    desenho.text((80, 80), "Empresa Exemplo Ltda - relatorio digitalizado", fill=0)
    for indice in range(12):
        y = 200 + indice * 60
        desenho.text((80, y), f"0{1 + indice % 9}/09/2026", fill=0)
        desenho.rectangle((300, y, 900, y + 14), fill=90)
        desenho.text((1000, y), f"{100 + indice},00", fill=0)
    buffer = io.BytesIO()
    imagem.save(buffer, format="PNG")
    buffer.seek(0)
    pdf = canvas.Canvas(str(destino / "escaneado.pdf"), pagesize=A4, invariant=1)
    pdf.drawImage(ImageReader(buffer), 0, 0, width=LARGURA, height=ALTURA)
    pdf.showPage()
    pdf.save()
    (destino / "escaneado.gabarito.json").write_text('{"total": 12}\n', encoding="utf-8")


def gerar(destino: Path = DESTINO_PADRAO) -> list[Path]:
    destino.mkdir(parents=True, exist_ok=True)
    gerar_layout_a(destino)
    gerar_layout_b(destino)
    gerar_layout_c(destino)
    gerar_escaneado(destino)
    return sorted(destino.glob("*.pdf"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--destino", type=Path, default=DESTINO_PADRAO)
    args = parser.parse_args()
    for pdf in gerar(args.destino):
        print(pdf.name)


if __name__ == "__main__":
    main()
