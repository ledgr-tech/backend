"""Leitura e validação do JSON da importação via IA (issue #158, ADR-013).

Sem banco: recebe os bytes do arquivo e devolve os lançamentos, ou levanta
um erro que a rota transforma em 422.

Entrada aceita, depois de tirar o BOM e os espaços das pontas: um JSON que
começa com `{` ou `[`, ou um único bloco de código markdown (```` ```json ````
ou ```` ``` ````) com nada fora dele. Texto solto antes ou depois é recusado:
o Ledgr não tenta achar JSON no meio de texto.

Três tipos de erro:

- `ArquivoCortado`: o conteúdo termina no meio (string, objeto ou lista sem
  fechar, ou bloco markdown sem o fechamento). É o caso da IA que parou
  antes do fim, e tem mensagem própria, fora da lista por campo.
- `ArquivoIlegivel`: o arquivo não chega a ser JSON (vazio, fora de UTF-8,
  texto fora do bloco, JSON quebrado com tudo fechado). Uma mensagem só.
- `ArquivoForaDoSchema`: JSON legível fora do schema v1 (schema_v1.json).
  Todos os problemas, até `MAXIMO_ERROS`, cada um com o caminho
  (`lancamentos[12].valor`, a partir de 0), o número do lançamento (a partir
  de 1) e uma mensagem em português. `total` conta todos.

`ler_campos` (issue #159) valida um lançamento só, no corpo da correção ou
da linha nova da revisão, com as mesmas regras e mensagens, e o caminho é o
nome do campo.

As mensagens repetem o que veio em `data` e `valor` (até 30 caracteres),
para a pessoa achar a linha no relatório, mas nunca a descrição. Nada disto
vai para log.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

VERSAO_SCHEMA = "1"
MAXIMO_ERROS = 50
TAMANHO_MAXIMO_DESCRICAO = 500

CAMPOS_DO_ARQUIVO = ("versao", "lancamentos")
CAMPOS_DO_LANCAMENTO = ("data", "valor", "descricao")

_DATA = re.compile(r"\d{4}-\d{2}-\d{2}")
_VALOR = re.compile(r"-?\d+(\.\d{1,2})?")
_DIGITOS_ANTES_DO_PONTO = 12
_ABERTURA_DO_BLOCO = re.compile(r"```[ \t]*(json)?[ \t]*", re.IGNORECASE)
_TAMANHO_DO_ECO = 30

ARQUIVO_CORTADO = (
    "O arquivo parece cortado no meio. A IA pode ter parado antes do fim: peça para ela "
    "continuar de onde parou e junte as partes, ou divida o PDF por período."
)
FORA_DO_SCHEMA = "O arquivo não está no formato do Ledgr. Corrija os pontos abaixo e envie de novo."


class ArquivoCortado(Exception):
    pass


class ArquivoIlegivel(Exception):
    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


@dataclass(frozen=True)
class ErroDeCampo:
    caminho: str
    lancamento: int | None
    mensagem: str


class ArquivoForaDoSchema(Exception):
    def __init__(self, erros: list[ErroDeCampo], total: int):
        super().__init__(f"{total} erro(s) de schema")
        self.erros = erros
        self.total = total


@dataclass(frozen=True)
class LancamentoImportado:
    posicao: int
    data: date
    valor: Decimal
    descricao: str

    @property
    def tipo(self) -> str:
        return "credito" if self.valor >= 0 else "debito"


class _Numero:
    """Número do JSON, guardado como veio, para a mensagem dizer o que chegou."""

    def __init__(self, texto: str):
        self.texto = texto


class _Objeto(dict):
    """Objeto do JSON que lembra as chaves repetidas (o `json` fica com a última)."""

    repetidas: list[str]


def _objeto(pares: list[tuple[str, object]]) -> _Objeto:
    objeto = _Objeto()
    objeto.repetidas = []
    for chave, valor in pares:
        if chave in objeto and chave not in objeto.repetidas:
            objeto.repetidas.append(chave)
        objeto[chave] = valor
    return objeto


def _texto(conteudo: bytes) -> str:
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArquivoIlegivel(
            "O arquivo precisa estar em UTF-8. Salve de novo o que a IA devolveu, como texto."
        ) from exc
    texto = texto.strip()
    if not texto:
        raise ArquivoIlegivel("O arquivo está vazio.")
    return texto


def _sem_o_bloco(texto: str) -> str:
    """O JSON de dentro do bloco markdown, ou o texto como veio quando não há bloco."""
    if not texto.startswith("```"):
        if texto[0] in "{[":
            return texto
        raise ArquivoIlegivel(
            "Envie só o JSON que a IA devolveu, sem texto antes ou depois. Se a IA escreveu "
            "uma explicação junto, copie só o bloco do JSON."
        )
    linhas = texto.split("\n")
    if not _ABERTURA_DO_BLOCO.fullmatch(linhas[0].strip()):
        raise ArquivoIlegivel(
            "O bloco de código deve começar com ``` ou ```json, numa linha só, e conter só o JSON."
        )
    fechamento = next((i for i in range(1, len(linhas)) if linhas[i].strip() == "```"), None)
    if fechamento is None:
        raise ArquivoCortado
    if any(linha.strip() for linha in linhas[fechamento + 1 :]):
        raise ArquivoIlegivel(
            "Envie só o JSON que a IA devolveu, sem texto antes ou depois. Se a IA escreveu "
            "uma explicação junto, copie só o bloco do JSON."
        )
    return "\n".join(linhas[1:fechamento]).strip()


def _termina_aberto(texto: str) -> bool:
    """O texto acaba com string, objeto ou lista sem fechar? Um fechamento
    trocado (`{]`) não é corte, é JSON quebrado."""
    pilha: list[str] = []
    em_string = escapado = False
    for caractere in texto:
        if em_string:
            if escapado:
                escapado = False
            elif caractere == "\\":
                escapado = True
            elif caractere == '"':
                em_string = False
        elif caractere == '"':
            em_string = True
        elif caractere in "{[":
            pilha.append("}" if caractere == "{" else "]")
        elif caractere in "}]" and (not pilha or pilha.pop() != caractere):
            return False
    return em_string or bool(pilha)


def _json(texto: str) -> object:
    try:
        return json.loads(
            texto,
            object_pairs_hook=_objeto,
            parse_float=_Numero,
            parse_int=_Numero,
            parse_constant=_Numero,
        )
    except json.JSONDecodeError as exc:
        if _termina_aberto(texto):
            raise ArquivoCortado from exc
        raise ArquivoIlegivel(
            f"O arquivo não é um JSON válido (linha {exc.lineno}, coluna {exc.colno}). Peça "
            "para a IA devolver só o JSON, no formato do prompt do Ledgr."
        ) from exc


def _numero(valor: object) -> bool:
    """Número do JSON: `_Numero` na leitura do arquivo, `int` ou `float` no
    corpo já lido pelo FastAPI (issue #159)."""
    return isinstance(valor, _Numero | int | float) and not isinstance(valor, bool)


def _eco(valor: object) -> str:
    if isinstance(valor, _Numero):
        bruto = valor.texto
    elif _numero(valor):
        bruto = str(valor)
    elif isinstance(valor, str):
        bruto = f'"{valor}"'
    elif isinstance(valor, bool):
        bruto = "true" if valor else "false"
    elif valor is None:
        bruto = "null"
    elif isinstance(valor, list):
        bruto = "uma lista"
    else:
        bruto = "um objeto"
    if len(bruto) > _TAMANHO_DO_ECO:
        bruto = bruto[: _TAMANHO_DO_ECO - 1] + "…"
    return bruto


class _Erros:
    def __init__(self) -> None:
        self.erros: list[ErroDeCampo] = []
        self.total = 0

    def __call__(self, caminho: str, mensagem: str, posicao: int | None = None) -> None:
        self.total += 1
        if len(self.erros) < MAXIMO_ERROS:
            numero = posicao + 1 if posicao is not None else None
            self.erros.append(ErroDeCampo(caminho=caminho, lancamento=numero, mensagem=mensagem))


def _campos(objeto: _Objeto, caminho: str, aceitos: tuple[str, ...], erro, posicao=None) -> None:
    prefixo = f"{caminho}." if caminho else ""
    for chave in objeto.repetidas:
        erro(f"{prefixo}{chave}", "campo repetido: deixe só um", posicao)
    for chave in objeto:
        if chave not in aceitos:
            lista = ", ".join(aceitos)
            erro(f"{prefixo}{chave}", f"campo não aceito: aqui o Ledgr só lê {lista}", posicao)
    for chave in aceitos:
        if chave not in objeto:
            erro(f"{prefixo}{chave}", "campo obrigatório", posicao)


def _data(valor: object, caminho: str, erro, posicao: int) -> date | None:
    if not isinstance(valor, str):
        erro(
            caminho,
            f'use texto entre aspas no formato AAAA-MM-DD, como "2026-09-05" (veio {_eco(valor)})',
            posicao,
        )
        return None
    if not _DATA.fullmatch(valor):
        erro(caminho, f'use o formato AAAA-MM-DD, como "2026-09-05" (veio {_eco(valor)})', posicao)
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        erro(caminho, f"essa data não existe (veio {_eco(valor)})", posicao)
        return None


def _valor(valor: object, caminho: str, erro, posicao: int) -> Decimal | None:
    if _numero(valor):
        erro(
            caminho,
            f'use o número entre aspas, como "-1234.56" (veio {_eco(valor)}, sem aspas)',
            posicao,
        )
        return None
    if not isinstance(valor, str) or not _VALOR.fullmatch(valor):
        erro(
            caminho,
            "use número com ponto decimal e no máximo duas casas, sem separador de milhar nem "
            f'R$, com sinal de menos na saída, como "-1234.56" (veio {_eco(valor)})',
            posicao,
        )
        return None
    if len(valor.lstrip("-").split(".")[0]) > _DIGITOS_ANTES_DO_PONTO:
        erro(caminho, f"valor grande demais (veio {_eco(valor)})", posicao)
        return None
    numero = Decimal(valor)
    if numero == 0:
        erro(caminho, "valor zero não é lançamento: saldos e totais não entram", posicao)
        return None
    return numero


def _descricao(valor: object, caminho: str, erro, posicao: int) -> str | None:
    if not isinstance(valor, str):
        erro(caminho, f"use texto entre aspas (veio {_eco(valor)})", posicao)
        return None
    texto = valor.strip()
    if not texto:
        erro(caminho, "a descrição está vazia", posicao)
        return None
    if len(texto) > TAMANHO_MAXIMO_DESCRICAO:
        erro(caminho, f"a descrição passa de {TAMANHO_MAXIMO_DESCRICAO} caracteres", posicao)
        return None
    return texto


def _versao(objeto: _Objeto, erro) -> bool:
    if "versao" not in objeto:
        return True  # o "campo obrigatório" já saiu em `_campos`
    versao = objeto["versao"]
    if versao == VERSAO_SCHEMA:
        return True
    if isinstance(versao, _Numero) and versao.texto == VERSAO_SCHEMA:
        erro("versao", f'use o texto "{VERSAO_SCHEMA}", entre aspas')
    else:
        erro(
            "versao",
            f"versão {_eco(versao)} não é aceita: use o prompt atual do Ledgr, que gera a "
            f'versão "{VERSAO_SCHEMA}"',
        )
    return False


def _lancamentos(lista: object, erro) -> list[LancamentoImportado]:
    if not isinstance(lista, list):
        erro("lancamentos", f"deve ser uma lista de lançamentos (veio {_eco(lista)})")
        return []
    if not lista:
        erro("lancamentos", "a lista está vazia: o relatório não tem nenhum lançamento?")
        return []
    lancamentos = []
    for posicao, item in enumerate(lista):
        caminho = f"lancamentos[{posicao}]"
        if not isinstance(item, _Objeto):
            erro(
                caminho,
                f"cada lançamento deve ser um objeto com data, valor e descricao "
                f"(veio {_eco(item)})",
                posicao,
            )
            continue
        _campos(item, caminho, CAMPOS_DO_LANCAMENTO, erro, posicao)
        data = _data(item["data"], f"{caminho}.data", erro, posicao) if "data" in item else None
        valor = (
            _valor(item["valor"], f"{caminho}.valor", erro, posicao) if "valor" in item else None
        )
        descricao = (
            _descricao(item["descricao"], f"{caminho}.descricao", erro, posicao)
            if "descricao" in item
            else None
        )
        if data is not None and valor is not None and descricao is not None:
            lancamentos.append(
                LancamentoImportado(posicao=posicao, data=data, valor=valor, descricao=descricao)
            )
    return lancamentos


def ler_importacao(conteudo: bytes) -> list[LancamentoImportado]:
    """Os lançamentos do arquivo, na ordem dele. Levanta `ArquivoCortado`,
    `ArquivoIlegivel` ou `ArquivoForaDoSchema`."""
    documento = _json(_sem_o_bloco(_texto(conteudo)))
    erro = _Erros()
    if not isinstance(documento, _Objeto):
        erro(
            "(arquivo)",
            'o arquivo deve ser um objeto JSON com "versao" e "lancamentos" '
            f"(veio {_eco(documento)})",
        )
        raise ArquivoForaDoSchema(erro.erros, erro.total)
    _campos(documento, "", CAMPOS_DO_ARQUIVO, erro)
    # Versão desconhecida: os lançamentos são de outro formato, e validá-los
    # pelo v1 só geraria ruído.
    if not _versao(documento, erro):
        raise ArquivoForaDoSchema(erro.erros, erro.total)
    lancamentos = _lancamentos(documento["lancamentos"], erro) if "lancamentos" in documento else []
    if erro.total:
        raise ArquivoForaDoSchema(erro.erros, erro.total)
    return lancamentos


_VALIDADORES = {"data": _data, "valor": _valor, "descricao": _descricao}


def ler_campos(corpo: dict, *, todos: bool, numero: int | None = None) -> dict:
    """Os campos de um lançamento da revisão (issue #159), validados como no
    schema v1: com `todos`, os três são obrigatórios (linha nova); sem, pelo
    menos um (correção). Levanta `ArquivoForaDoSchema` com o nome do campo no
    caminho e `numero` (o do lançamento corrigido, a partir de 1) no erro."""
    posicao = numero - 1 if numero is not None else None
    erro = _Erros()
    for chave in corpo:
        if chave not in CAMPOS_DO_LANCAMENTO:
            lista = ", ".join(CAMPOS_DO_LANCAMENTO)
            erro(chave, f"campo não aceito: aqui o Ledgr só lê {lista}", posicao)
    if todos:
        for chave in CAMPOS_DO_LANCAMENTO:
            if chave not in corpo:
                erro(chave, "campo obrigatório", posicao)
    elif not any(chave in corpo for chave in CAMPOS_DO_LANCAMENTO):
        erro("(lançamento)", "informe data, valor ou descricao para corrigir", posicao)
    lidos = {}
    for chave, validar in _VALIDADORES.items():
        if chave in corpo:
            lido = validar(corpo[chave], chave, erro, posicao)
            if lido is not None:
                lidos[chave] = lido
    if erro.total:
        raise ArquivoForaDoSchema(erro.erros, erro.total)
    return lidos
