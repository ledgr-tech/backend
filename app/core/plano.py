"""Plano da empresa e período de teste (issue #122): a regra, sem banco.

`PLANOS` é a lista única dos planos escolhíveis; o CHECK de `empresas.plano`,
o corpo do `POST /empresa/plano` e o script de ativação saem dela. "Volume"
não está aqui: é contato comercial. Nenhum preço mora no backend.

`situacao_da_empresa` é a única conta da situação, usada pelo `GET /me`, pelo
`POST /empresa/plano` e pelo bloqueio do `POST /conciliacoes`, para os três
nunca divergirem. É calculada, nunca gravada:

- "ativo": `assinatura_ativa` (hoje, marcada à mão por scripts/ativar_empresa.py
  até o gateway existir);
- "sem_plano": sem plano escolhido;
- "teste": antes de `teste_termina_em`;
- "teste_encerrado": de `teste_termina_em` em diante.

O "agora" é o relógio da aplicação da #75 (`app.core.auth._agora`, em UTC),
o mesmo das sessões; chamado pelo módulo, para os testes congelarem um só.
"""

from datetime import datetime, timedelta
from typing import Literal, Protocol

PLANOS = ("essencial", "padrao", "avancado")
Plano = Literal["essencial", "padrao", "avancado"]
DURACAO_TESTE = timedelta(days=14)

Situacao = Literal["sem_plano", "teste", "teste_encerrado", "ativo"]


class _ComPlano(Protocol):
    plano: str | None
    teste_termina_em: datetime | None
    assinatura_ativa: bool


def agora() -> datetime:
    # Import aqui: app.core.auth importa os models, e o model de empresa
    # importa `PLANOS` deste módulo.
    from app.core import auth

    return auth._agora()


def situacao_da_empresa(empresa: _ComPlano, instante: datetime | None = None) -> Situacao:
    if empresa.assinatura_ativa:
        return "ativo"
    if empresa.plano is None or empresa.teste_termina_em is None:
        return "sem_plano"
    if (instante or agora()) < empresa.teste_termina_em:
        return "teste"
    return "teste_encerrado"
