"""Tipos e interface da consulta de CNPJ na Receita (issue #119).

`ConsultaCNPJ` é a interface que qualquer provedor precisa cumprir, como
`ProvedorDeEmail` (ADR-012) e o provedor de IA (ADR-011). Hoje só existe a
BrasilAPI (app/services/consulta_cnpj/brasilapi.py).

O resultado é sempre categorizado, nunca texto livre do provedor:
- "ativa": a situação cadastral é ATIVA;
- "inativa": qualquer outra situação (NULA, SUSPENSA, INAPTA, BAIXADA...);
- "nao_encontrado": o provedor diz que o CNPJ não existe na base dele;
- "indisponivel": não deu para saber (consulta desligada, timeout, erro de
  rede, 5xx, 429, corpo fora do formato ou CNPJ recusado pelo formato).

A razão social vem só com "ativa" e "inativa". `consultar` nunca levanta:
qualquer falha vira "indisponivel".
"""

from dataclasses import dataclass
from typing import Literal, Protocol

SituacaoCNPJ = Literal["ativa", "inativa", "nao_encontrado", "indisponivel"]


@dataclass(frozen=True)
class ResultadoConsultaCNPJ:
    situacao: SituacaoCNPJ
    razao_social: str | None = None


INDISPONIVEL = ResultadoConsultaCNPJ(situacao="indisponivel")
NAO_ENCONTRADO = ResultadoConsultaCNPJ(situacao="nao_encontrado")


class ConsultaCNPJ(Protocol):
    """Interface de quem consulta a situação de um CNPJ na Receita (issue #119)."""

    def consultar(self, cnpj: str) -> ResultadoConsultaCNPJ:
        """`cnpj` já normalizado (app/core/cnpj.py): 14 caracteres, sem máscara."""
        ...
