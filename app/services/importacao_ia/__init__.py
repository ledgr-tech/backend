"""Importação do relatório de lançamentos via IA (issue #158, ADR-013).

O usuário cola o prompt do Ledgr e o PDF do sistema de gestão na IA que já
usa e importa o JSON que ela devolve. O Ledgr não chama nenhuma IA: só serve
o prompt e recebe o arquivo.

- `leitura.py`: lê e valida o JSON (schema v1, `schema_v1.json`), sem banco.
- `rascunho.py`: grava, encontra, descarta e expira o rascunho.
- `prompt.md`: o texto do prompt, com `versao_schema` e `versao_prompt` no
  cabeçalho. `prompt_atual()` lê os dois e o texto; `schema_atual()`, o
  schema. Os dois arquivos são lidos uma vez por processo.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.services.importacao_ia.leitura import VERSAO_SCHEMA

_PASTA = Path(__file__).resolve().parent
_CABECALHO = re.compile(r"\A<!--(?P<cabecalho>.*?)-->\s*", re.DOTALL)
_VERSAO = re.compile(r"^(versao_\w+):\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    versao_schema: str
    versao_prompt: str
    texto: str


@cache
def prompt_atual() -> Prompt:
    bruto = (_PASTA / "prompt.md").read_text(encoding="utf-8")
    cabecalho = _CABECALHO.match(bruto)
    if cabecalho is None:
        raise RuntimeError("prompt.md sem o cabeçalho com as versões")
    versoes = dict(_VERSAO.findall(cabecalho["cabecalho"]))
    prompt = Prompt(
        versao_schema=versoes["versao_schema"],
        versao_prompt=versoes["versao_prompt"],
        texto=bruto[cabecalho.end() :].strip(),
    )
    if prompt.versao_schema != VERSAO_SCHEMA:
        raise RuntimeError("prompt.md pede uma versão de schema que a leitura não aceita")
    return prompt


@cache
def schema_atual() -> dict:
    return json.loads((_PASTA / f"schema_v{VERSAO_SCHEMA}.json").read_text(encoding="utf-8"))
