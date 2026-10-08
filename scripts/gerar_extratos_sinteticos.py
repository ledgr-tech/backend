#!/usr/bin/env python3
"""Gerador de extratos sintéticos (OFX/CSV) pra fixtures de teste (issue #10).

Ferramenta de apoio, não fixture de CI: os testes automatizados de parsing
(#8/#9, e a #11 que vem a seguir) continuam usando as fixtures manuais e
determinísticas já commitadas em tests/fixtures/ (extrato_valido.ofx,
extrato_valido.csv, extrato_valido_latin1.csv). Este script serve pra gerar
volume (Sprint 6: teste de volume com 10-20 mil lançamentos) e variedade
pra QA manual/exploração — a saída vai pra tests/fixtures/gerados/, que é
gitignored (ver .gitignore).

Uso:
    python scripts/gerar_extratos_sinteticos.py --formato ofx --quantidade 500
    python scripts/gerar_extratos_sinteticos.py --formato csv --encoding latin-1 --seed 42
    python scripts/gerar_extratos_sinteticos.py --formato csv --corromper
    python scripts/gerar_extratos_sinteticos.py --formato ofx --quantidade 20000 --saida /tmp/volume

Modo de par (issue #16, motor de matching exato da Sprint 3): `--par` gera
dois arquivos, um "banco" e um "sistema", com `--quantidade` lançamentos cada,
e `--sobreposicao N` define quantos lançamentos são idênticos (valor, data e
descrição) nos dois. Os lançamentos não sobrepostos nunca colidem em (valor,
data) com nenhum lançamento do outro lado (regenera em caso de colisão,
determinístico pela seed), então o número esperado de `match_exato` é
exatamente N, e o script imprime esse número:
    python scripts/gerar_extratos_sinteticos.py --formato csv --par --quantidade 50 --sobreposicao 30 --seed 7

O `--seed` garante reprodutibilidade determinística (mesma seed + mesmos
argumentos geram exatamente os mesmos lançamentos), inclusive no modo de par.
"""

import argparse
import csv
import io
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Descrições plausíveis de extrato bancário brasileiro, já pensando nas
# categorias de divergência da Sprint 4 (07-tecnico/arquitetura-tecnica.md):
# tarifa bancária como categoria própria, PIX/transferência/folha como
# exemplos comuns de lançamento que o motor de matching vai precisar
# comparar. Lista curta hardcoded — sem adicionar Faker só pra isso.
_NOMES = [
    "João Silva",
    "Maria Santos",
    "Comércio ABC Ltda",
    "Fornecedor XYZ",
    "Consultoria Delta",
    "Indústria Beta S.A.",
    "Transportadora Rápida",
    "Ana Oliveira",
    "Carlos Pereira",
    "Distribuidora Central",
]
_ESTABELECIMENTOS = [
    "Supermercado Extra",
    "Posto Ipiranga",
    "Farmácia São Paulo",
    "Papelaria Central",
    "Restaurante Sabor Caseiro",
    "Loja de Materiais de Construção",
]
_SERVICOS = ["internet", "água", "energia elétrica", "telefonia"]

_DESCRICOES_CREDITO = [
    lambda r: f"PIX recebido de {r.choice(_NOMES)}",
    lambda r: f"Transferência recebida de {r.choice(_NOMES)}",
    lambda r: f"Recebimento de cliente {r.choice(_NOMES)}",
    lambda r: "Depósito em dinheiro",
    lambda r: "Estorno de compra",
    lambda r: "Rendimento de aplicação financeira",
]
_DESCRICOES_DEBITO = [
    lambda r: f"Compra cartão de débito - {r.choice(_ESTABELECIMENTOS)}",
    lambda r: f"PIX enviado para {r.choice(_NOMES)}",
    lambda r: f"Pagamento de fornecedor {r.choice(_NOMES)}",
    lambda r: "Tarifa bancária - manutenção de conta",
    lambda r: "Folha de pagamento",
    lambda r: f"Débito automático - {r.choice(_SERVICOS)}",
    lambda r: "Saque em caixa eletrônico",
    lambda r: "Pagamento de boleto",
]

_DATA_BASE = date(2026, 9, 1)
_JANELA_DIAS = 30

# Nomes de codec do Python pra cada opção de --encoding. "latin-1" é o nome
# comum informal; "iso-8859-1" é o codec real do Python (mesma tabela).
_ENCODINGS = {"utf-8": "utf-8", "latin-1": "iso-8859-1", "cp1252": "cp1252"}


def _hoje() -> date:
    return datetime.now(tz=timezone.utc).date()


class Lancamento:
    """Lançamento sintético em memória, antes de virar OFX ou CSV."""

    def __init__(self, data: date, valor: Decimal, descricao: str, tipo: str) -> None:
        self.data = data
        self.valor = valor
        self.descricao = descricao
        self.tipo = tipo  # "credito" | "debito"


def _gerar_lancamento(rng: random.Random) -> Lancamento:
    data = _DATA_BASE + timedelta(days=rng.randint(0, _JANELA_DIAS - 1))
    tipo = rng.choice(["credito", "debito"])
    if tipo == "credito":
        descricao = rng.choice(_DESCRICOES_CREDITO)(rng)
        valor = Decimal(rng.randint(1_000, 500_000)) / 100
    else:
        descricao = rng.choice(_DESCRICOES_DEBITO)(rng)
        valor = -(Decimal(rng.randint(500, 300_000)) / 100)
    return Lancamento(data=data, valor=valor, descricao=descricao, tipo=tipo)


def _gerar_lancamentos(quantidade: int, rng: random.Random) -> list[Lancamento]:
    return [_gerar_lancamento(rng) for _ in range(quantidade)]


def _montar_ofx(lancamentos: list[Lancamento]) -> str:
    """Monta o texto OFX no mesmo formato da fixture manual (tests/fixtures/extrato_valido.ofx)."""
    transacoes = []
    for indice, lancamento in enumerate(lancamentos, start=1):
        trntype = "CREDIT" if lancamento.tipo == "credito" else "DEBIT"
        fitid = f"{lancamento.data:%Y%m%d}{indice:06d}"
        transacoes.append(
            "<STMTTRN>\n"
            f"<TRNTYPE>{trntype}\n"
            f"<DTPOSTED>{lancamento.data:%Y%m%d}\n"
            f"<TRNAMT>{lancamento.valor}\n"
            f"<FITID>{fitid}\n"
            f"<MEMO>{lancamento.descricao}\n"
            "</STMTTRN>"
        )
    corpo_transacoes = "\n".join(transacoes)

    if lancamentos:
        dtstart = min(lancamento.data for lancamento in lancamentos)
        dtend = max(lancamento.data for lancamento in lancamentos)
    else:
        dtstart = dtend = _DATA_BASE

    return (
        "OFXHEADER:100\n"
        "DATA:OFXSGML\n"
        "VERSION:102\n"
        "SECURITY:NONE\n"
        "ENCODING:USASCII\n"
        "CHARSET:1252\n"
        "COMPRESSION:NONE\n"
        "OLDFILEUID:NONE\n"
        "NEWFILEUID:NONE\n"
        "\n"
        "<OFX>\n"
        "<SIGNONMSGSRSV1>\n"
        "<SONRS>\n"
        "<STATUS>\n"
        "<CODE>0\n"
        "<SEVERITY>INFO\n"
        "</STATUS>\n"
        f"<DTSERVER>{_hoje():%Y%m%d}120000\n"
        "<LANGUAGE>POR\n"
        "</SONRS>\n"
        "</SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1>\n"
        "<STMTTRNRS>\n"
        "<TRNUID>1\n"
        "<STATUS>\n"
        "<CODE>0\n"
        "<SEVERITY>INFO\n"
        "</STATUS>\n"
        "<STMTRS>\n"
        "<CURDEF>BRL\n"
        "<BANKACCTFROM>\n"
        "<BANKID>001\n"
        "<ACCTID>1234567\n"
        "<ACCTTYPE>CHECKING\n"
        "</BANKACCTFROM>\n"
        "<BANKTRANLIST>\n"
        f"<DTSTART>{dtstart:%Y%m%d}\n"
        f"<DTEND>{dtend:%Y%m%d}\n"
        f"{corpo_transacoes}\n"
        "</BANKTRANLIST>\n"
        "<LEDGERBAL>\n"
        "<BALAMT>0.00\n"
        f"<DTASOF>{_hoje():%Y%m%d}120000\n"
        "</LEDGERBAL>\n"
        "</STMTRS>\n"
        "</STMTTRNRS>\n"
        "</BANKMSGSRSV1>\n"
        "</OFX>\n"
    )


def _corromper_ofx(texto_valido: str) -> str:
    """Header malformado: remove a linha `OFXHEADER:100` (obrigatória, primeira do arquivo).

    Reproduz o erro real que o ofxtools levanta (`OFXHeaderError`), já
    coberto pelo tratamento de erro do parser (app/parsers/ofx.py).
    """
    linhas = texto_valido.splitlines(keepends=True)
    return "".join(linha for linha in linhas if not linha.startswith("OFXHEADER:"))


def _montar_csv(lancamentos: list[Lancamento]) -> str:
    buffer = io.StringIO()
    escritor = csv.writer(buffer)
    escritor.writerow(["Data", "Valor", "Descricao"])
    for lancamento in lancamentos:
        escritor.writerow(
            [f"{lancamento.data:%Y-%m-%d}", str(lancamento.valor), lancamento.descricao]
        )
    return buffer.getvalue()


def _corromper_csv(texto_valido: str) -> str:
    """Linha truncada: corta o arquivo no meio de um campo de texto entre aspas.

    Simula um arquivo cortado no meio da escrita/download (aspas nunca
    fecham, arquivo termina abruptamente) — reproduz o
    `pandas.errors.ParserError` ("EOF inside string") já tratado pelo
    parser (app/parsers/csv.py).
    """
    linhas = texto_valido.splitlines()
    cabecalho = linhas[0]
    primeira_linha_valida = linhas[1] if len(linhas) > 1 else "2026-09-05,1500.00,X"
    data_bruta, valor_bruto, _ = primeira_linha_valida.split(",", 2)
    return f'{cabecalho}\n{data_bruta},{valor_bruto},"Descricao truncada no meio do arquivo'


_MAX_TENTATIVAS_SEM_COLISAO = 1000


def _gerar_sem_colisao(
    quantidade: int, chaves_proibidas: set[tuple[Decimal, date]], rng: random.Random
) -> list[Lancamento]:
    """Gera `quantidade` lançamentos cuja chave (valor, data) não está em
    `chaves_proibidas`, regenerando o que colidir (determinístico pela seed)."""
    gerados = []
    for _ in range(quantidade):
        for _ in range(_MAX_TENTATIVAS_SEM_COLISAO):
            lancamento = _gerar_lancamento(rng)
            if (lancamento.valor, lancamento.data) not in chaves_proibidas:
                gerados.append(lancamento)
                break
        else:
            raise RuntimeError("Não foi possível gerar lançamento sem colisão de (valor, data).")
    return gerados


def _gerar_par(
    quantidade: int, sobreposicao: int, rng: random.Random
) -> tuple[list[Lancamento], list[Lancamento]]:
    """Gera (banco, sistema) com `quantidade` lançamentos cada e exatamente
    `sobreposicao` idênticos nos dois. Os demais não colidem em (valor, data)
    com nenhum lançamento do outro lado, então o número de matches exatos
    esperado é `sobreposicao`."""
    comuns = _gerar_lancamentos(sobreposicao, rng)
    chaves_comuns = {(item.valor, item.data) for item in comuns}

    so_banco = _gerar_sem_colisao(quantidade - sobreposicao, chaves_comuns, rng)
    chaves_banco = chaves_comuns | {(item.valor, item.data) for item in so_banco}
    so_sistema = _gerar_sem_colisao(quantidade - sobreposicao, chaves_banco, rng)

    banco = comuns + so_banco
    sistema = list(comuns) + so_sistema
    rng.shuffle(banco)
    rng.shuffle(sistema)
    return banco, sistema


def _serializar(
    formato: str, lancamentos: list[Lancamento], encoding: str, corromper: bool
) -> bytes:
    if formato == "ofx":
        texto = _montar_ofx(lancamentos)
        if corromper:
            texto = _corromper_ofx(texto)
        # OFX sempre grava em cp1252 (CHARSET:1252 declarado no header),
        # igual a fixture manual — --encoding não se aplica a este formato.
        return texto.encode("cp1252")

    texto = _montar_csv(lancamentos)
    if corromper:
        texto = _corromper_csv(texto)
    return texto.encode(_ENCODINGS[encoding])


def _gerar_conteudo(
    formato: str, quantidade: int, encoding: str, corromper: bool, rng: random.Random
) -> bytes:
    lancamentos = _gerar_lancamentos(quantidade, rng)
    return _serializar(formato, lancamentos, encoding, corromper)


def _nome_arquivo_par(
    lado: str, formato: str, quantidade: int, sobreposicao: int, encoding: str, seed: int
) -> str:
    partes = ["extrato", "par", lado, formato, f"qtd{quantidade}", f"sobreposicao{sobreposicao}"]
    if formato == "csv":
        partes.append(encoding.replace("-", ""))
    partes.append(f"seed{seed}")
    return "_".join(partes) + f".{formato}"


def _nome_arquivo(formato: str, quantidade: int, encoding: str, corromper: bool, seed: int) -> str:
    partes = ["extrato"]
    if corromper:
        partes.append("corrompido")
    partes.append(formato)
    partes.append(f"qtd{quantidade}")
    if formato == "csv":
        partes.append(encoding.replace("-", ""))
    partes.append(f"seed{seed}")
    extensao = "ofx" if formato == "ofx" else "csv"
    return "_".join(partes) + f".{extensao}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera extratos sintéticos (OFX/CSV) pra fixtures de teste/QA manual."
    )
    parser.add_argument("--formato", required=True, choices=["ofx", "csv"])
    parser.add_argument(
        "--quantidade", type=int, default=20, help="Número de lançamentos no extrato (default: 20)."
    )
    parser.add_argument(
        "--encoding",
        choices=sorted(_ENCODINGS),
        default="utf-8",
        help="Só se aplica a --formato csv (default: utf-8).",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Pra reprodutibilidade (default: aleatória)."
    )
    parser.add_argument(
        "--corromper",
        action="store_true",
        help="Gera arquivo propositalmente inválido (header malformado/linha truncada), pra issue #11.",
    )
    parser.add_argument(
        "--par",
        action="store_true",
        help="Gera dois arquivos (banco e sistema) com sobreposição controlada (issue #16).",
    )
    parser.add_argument(
        "--sobreposicao",
        type=int,
        default=None,
        help="Só com --par: quantos lançamentos são idênticos nos dois arquivos.",
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=Path("tests/fixtures/gerados"),
        help="Diretório de destino (default: tests/fixtures/gerados/, gitignored).",
    )
    args = parser.parse_args(argv)

    if args.quantidade < 1:
        parser.error("--quantidade deve ser >= 1")
    if args.par:
        if args.corromper:
            parser.error("--corromper não se combina com --par")
        if args.sobreposicao is None:
            parser.error("--par exige --sobreposicao")
        if not 0 <= args.sobreposicao <= args.quantidade:
            parser.error("--sobreposicao deve estar entre 0 e --quantidade")
    elif args.sobreposicao is not None:
        parser.error("--sobreposicao só se aplica com --par")
    if args.encoding != "utf-8" and args.formato == "ofx":
        print(
            "aviso: --encoding é ignorado para --formato ofx (sempre grava em cp1252)",
            file=sys.stderr,
        )
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)
    rng = random.Random(seed)

    if args.par:
        _main_par(args, seed, rng)
        return

    conteudo = _gerar_conteudo(args.formato, args.quantidade, args.encoding, args.corromper, rng)

    args.saida.mkdir(parents=True, exist_ok=True)
    nome_arquivo = _nome_arquivo(args.formato, args.quantidade, args.encoding, args.corromper, seed)
    caminho = args.saida / nome_arquivo
    caminho.write_bytes(conteudo)

    print(
        f"Gerado: {caminho} ({len(conteudo)} bytes, formato={args.formato}, "
        f"quantidade={args.quantidade}, encoding={args.encoding if args.formato == 'csv' else 'cp1252'}, "
        f"corrompido={args.corromper}, seed={seed})"
    )


def _main_par(args: argparse.Namespace, seed: int, rng: random.Random) -> None:
    banco, sistema = _gerar_par(args.quantidade, args.sobreposicao, rng)
    args.saida.mkdir(parents=True, exist_ok=True)

    caminhos = []
    for lado, lancamentos in (("banco", banco), ("sistema", sistema)):
        conteudo = _serializar(args.formato, lancamentos, args.encoding, corromper=False)
        nome = _nome_arquivo_par(
            lado, args.formato, args.quantidade, args.sobreposicao, args.encoding, seed
        )
        caminho = args.saida / nome
        caminho.write_bytes(conteudo)
        caminhos.append(caminho)

    print(
        f"Gerado par: banco={caminhos[0]} sistema={caminhos[1]} (formato={args.formato}, "
        f"quantidade={args.quantidade}, sobreposicao={args.sobreposicao}, seed={seed})"
    )
    print(f"matches_esperados={args.sobreposicao}")


if __name__ == "__main__":
    main()
