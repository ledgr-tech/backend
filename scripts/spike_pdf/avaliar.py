"""Avalia a extração de PDF contra o gabarito de cada amostra (issue #84).

    python -m scripts.spike_pdf.avaliar --amostras <pasta> --estrategia tabela|texto|todas \\
        --saida <pasta> [--limiar 0.8]

Para cada PDF da pasta com gabarito ao lado (`<nome>.gabarito.csv` ou
`<nome>.gabarito.json`), roda as estratégias pedidas e grava em `--saida`:

- `relatorio.md`: uma tabela por amostra e um quadro geral comparando as
  estratégias;
- `relatorio.json`: os mesmos números;
- `<amostra>.<estrategia>.diferencas.csv`: as linhas que faltaram, as que
  sobraram e as com valor errado ou sinal invertido.

O terminal só mostra números: descrições de lançamento vão apenas para os
arquivos de saída.
"""

import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from scripts.spike_pdf.extracao import (
    ESTRATEGIAS,
    PdfSemTexto,
    carregar_layout,
    extrair,
)
from scripts.spike_pdf.gabarito import caminho_do_gabarito, ler_gabarito
from scripts.spike_pdf.pareamento import LIMIAR_PADRAO, Metricas, metricas, parear

LIMITE_FRONT_BYTES = 4 * 1024 * 1024


@dataclass
class ResultadoAmostra:
    amostra: str
    estrategia: str
    paginas: int | None
    tamanho_bytes: int
    segundos: float | None
    configuracao: str | None
    gabarito_so_total: bool
    total_gabarito: int
    total_extraido: int | None
    metricas: Metricas | None
    soma_gabarito: Decimal | None
    soma_extraida: Decimal | None
    fora_do_escopo: str | None = None
    avisos: list[str] = field(default_factory=list)

    def como_dict(self) -> dict:
        dados = {
            "amostra": self.amostra,
            "estrategia": self.estrategia,
            "paginas": self.paginas,
            "tamanho_bytes": self.tamanho_bytes,
            "acima_do_limite_do_front": self.tamanho_bytes > LIMITE_FRONT_BYTES,
            "segundos": self.segundos,
            "configuracao_especifica": self.configuracao,
            "gabarito_so_total": self.gabarito_so_total,
            "total_gabarito": self.total_gabarito,
            "total_extraido": self.total_extraido,
            "soma_gabarito": str(self.soma_gabarito) if self.soma_gabarito is not None else None,
            "soma_extraida": str(self.soma_extraida) if self.soma_extraida is not None else None,
            "fora_do_escopo": self.fora_do_escopo,
            "avisos": self.avisos,
        }
        if self.metricas is not None:
            m = self.metricas
            dados.update(
                corretas=m.corretas,
                recall=m.recall,
                precisao=m.precisao,
                valor_errado=m.valor_errado,
                sinal_invertido=m.sinal_invertido,
                faltando=m.faltando,
                sobrando=m.sobrando,
            )
        return dados


def _diferencas(caminho: Path, resultado) -> None:
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        escritor.writerow(
            [
                "tipo",
                "data",
                "descricao_gabarito",
                "valor_gabarito",
                "descricao_extraida",
                "valor_extraido",
            ]
        )
        for tipo, pares in (
            ("valor_errado", resultado.valor_errado),
            ("sinal_invertido", resultado.sinal_invertido),
        ):
            for esperado, obtido in pares:
                escritor.writerow(
                    [
                        tipo,
                        esperado.data,
                        esperado.descricao,
                        esperado.valor,
                        obtido.descricao,
                        obtido.valor,
                    ]
                )
        for lancamento in resultado.faltando:
            escritor.writerow(
                ["faltando", lancamento.data, lancamento.descricao, lancamento.valor, "", ""]
            )
        for lancamento in resultado.sobrando:
            escritor.writerow(
                ["sobrando", lancamento.data, "", "", lancamento.descricao, lancamento.valor]
            )


def avaliar_amostra(pdf: Path, estrategia: str, saida: Path, limiar: float) -> ResultadoAmostra:
    gabarito = ler_gabarito(caminho_do_gabarito(pdf))
    layout = carregar_layout(pdf)
    base = {
        "amostra": pdf.name,
        "estrategia": estrategia,
        "tamanho_bytes": pdf.stat().st_size,
        "configuracao": layout.nome,
        "gabarito_so_total": gabarito.so_total,
        "total_gabarito": gabarito.total,
        "soma_gabarito": (
            sum((lanc.valor for lanc in gabarito.lancamentos), Decimal(0))
            if gabarito.lancamentos is not None
            else None
        ),
    }
    inicio = time.perf_counter()
    try:
        extracao = extrair(pdf, estrategia, layout)
    except PdfSemTexto as erro:
        return ResultadoAmostra(
            **base,
            paginas=None,
            segundos=None,
            total_extraido=None,
            metricas=None,
            soma_extraida=None,
            fora_do_escopo=str(erro),
        )
    segundos = time.perf_counter() - inicio

    avisos = list(extracao.avisos)
    if gabarito.so_total:
        avisos.append("gabarito só com o total: medição fraca, compara apenas quantidades")
        medidas = None
    else:
        resultado = parear(gabarito.lancamentos, extracao.lancamentos, limiar)
        medidas = metricas(gabarito.lancamentos, extracao.lancamentos, resultado)
        _diferencas(saida / f"{pdf.stem}.{estrategia}.diferencas.csv", resultado)
    if base["tamanho_bytes"] > LIMITE_FRONT_BYTES:
        avisos.append("arquivo acima de 4 MB, o limite de upload do front")
    return ResultadoAmostra(
        **base,
        paginas=extracao.paginas,
        segundos=round(segundos, 3),
        total_extraido=len(extracao.lancamentos),
        metricas=medidas,
        soma_extraida=sum((lanc.valor for lanc in extracao.lancamentos), Decimal(0)),
        avisos=avisos,
    )


def _pct(valor: float | None) -> str:
    return "-" if valor is None else f"{valor:.1%}"


def _markdown(resultados: list[ResultadoAmostra], limiar: float) -> str:
    linhas = [
        "# Relatório do spike de PDF (issue #84)",
        "",
        f"Pareamento: mesma data, mesmo valor (com sinal) e descrição com similaridade >= {limiar}.",
        "Erro de valor (valor errado ou sinal invertido) é o pior caso para a conciliação.",
        "",
        "## Quadro geral",
        "",
        (
            "| Amostra | Estratégia | Gabarito | Extraídas | Corretas | Recall | Precisão "
            "| Valor errado | Sinal invertido | Config. específica |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in resultados:
        if r.fora_do_escopo:
            linhas.append(
                f"| {r.amostra} | {r.estrategia} | {r.total_gabarito} | - | - | - | - | - | - "
                f"| {r.fora_do_escopo} |"
            )
            continue
        m = r.metricas
        if m is None:
            linhas.append(
                f"| {r.amostra} | {r.estrategia} | {r.total_gabarito} | {r.total_extraido} "
                f"| - | - | - | - | - | {'sim' if r.configuracao else 'não'} (só total) |"
            )
            continue
        erro = lambda n: f"**{n}**" if n else "0"
        linhas.append(
            f"| {r.amostra} | {r.estrategia} | {m.gabarito} | {m.extraidas} | {m.corretas} "
            f"| {_pct(m.recall)} | {_pct(m.precisao)} | {erro(m.valor_errado)} "
            f"| {erro(m.sinal_invertido)} | {'sim' if r.configuracao else 'não'} |"
        )
    linhas += ["", "## Por amostra", ""]
    for r in resultados:
        linhas += [f"### {r.amostra}, estratégia {r.estrategia}", ""]
        if r.fora_do_escopo:
            linhas += [f"{r.fora_do_escopo}.", ""]
            continue
        linhas += [
            "| Medida | Valor |",
            "|---|---|",
            f"| Páginas | {r.paginas} |",
            (
                f"| Tamanho | {r.tamanho_bytes / 1024:.0f} KB"
                f"{' (acima de 4 MB)' if r.tamanho_bytes > LIMITE_FRONT_BYTES else ''} |"
            ),
            f"| Tempo de extração | {r.segundos} s |",
            f"| Configuração específica | {r.configuracao or 'não'} |",
            f"| Linhas no gabarito | {r.total_gabarito} |",
            f"| Linhas extraídas | {r.total_extraido} |",
        ]
        if r.metricas is not None:
            m = r.metricas
            linhas += [
                f"| Corretas | {m.corretas} |",
                f"| Recall | {_pct(m.recall)} |",
                f"| Precisão | {_pct(m.precisao)} |",
                f"| **Valor errado** | {m.valor_errado} |",
                f"| **Sinal invertido** | {m.sinal_invertido} |",
                f"| Faltando | {m.faltando} |",
                f"| Sobrando | {m.sobrando} |",
                f"| Soma do gabarito | {r.soma_gabarito} |",
                f"| Soma extraída | {r.soma_extraida} |",
            ]
        else:
            linhas.append(f"| Soma extraída | {r.soma_extraida} |")
        linhas.append("")
        for aviso in r.avisos:
            linhas.append(f"- Aviso: {aviso}")
        if r.avisos:
            linhas.append("")
    return "\n".join(linhas) + "\n"


def avaliar(amostras: Path, estrategias: list[str], saida: Path, limiar: float = LIMIAR_PADRAO):
    saida.mkdir(parents=True, exist_ok=True)
    resultados = []
    for pdf in sorted(amostras.glob("*.pdf")):
        if caminho_do_gabarito(pdf) is None:
            print(f"{pdf.name}: sem gabarito, ignorado")
            continue
        for estrategia in estrategias:
            resultados.append(avaliar_amostra(pdf, estrategia, saida, limiar))
    (saida / "relatorio.md").write_text(_markdown(resultados, limiar), encoding="utf-8")
    (saida / "relatorio.json").write_text(
        json.dumps([r.como_dict() for r in resultados], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return resultados


def main() -> None:
    parser = argparse.ArgumentParser(description="Avalia a extração de PDF contra o gabarito.")
    parser.add_argument("--amostras", type=Path, required=True)
    parser.add_argument("--estrategia", choices=[*ESTRATEGIAS, "todas"], default="todas")
    parser.add_argument("--saida", type=Path, required=True)
    parser.add_argument("--limiar", type=float, default=LIMIAR_PADRAO)
    args = parser.parse_args()
    estrategias = list(ESTRATEGIAS) if args.estrategia == "todas" else [args.estrategia]
    for r in avaliar(args.amostras, estrategias, args.saida, args.limiar):
        if r.fora_do_escopo:
            print(f"{r.amostra} [{r.estrategia}]: fora do escopo (sem texto)")
        elif r.metricas is None:
            print(
                f"{r.amostra} [{r.estrategia}]: gabarito {r.total_gabarito}, "
                f"extraídas {r.total_extraido} (só total)"
            )
        else:
            m = r.metricas
            print(
                f"{r.amostra} [{r.estrategia}]: recall {_pct(m.recall)}, precisão "
                f"{_pct(m.precisao)}, valor errado {m.valor_errado}, sinal invertido "
                f"{m.sinal_invertido}"
            )
    print(f"Relatório em {args.saida / 'relatorio.md'}")


if __name__ == "__main__":
    main()
