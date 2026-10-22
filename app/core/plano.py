"""Plano da conta e período de teste (issue #122): a regra, sem banco. Desde a
issue #133 (ADR-019), o plano é da conta, e não mais da empresa.

`PLANOS` é a lista única dos planos escolhíveis; o CHECK de `contas.plano`,
o corpo do `POST /empresa/plano` e o script de ativação saem dela. "Volume"
não está aqui: é contato comercial. Nenhum preço mora no backend.

`situacao_da_conta` é a única conta da situação, usada pelo `GET /me`, pelo
`POST /empresa/plano` e pelo bloqueio do `POST /conciliacoes`, para os três
nunca divergirem. É calculada, nunca gravada:

- "ativo": `assinatura_ativa` (hoje, marcada à mão por scripts/ativar_empresa.py
  até o gateway existir);
- "sem_plano": sem plano escolhido;
- "teste": antes de `teste_termina_em`;
- "teste_encerrado": de `teste_termina_em` em diante.

O "agora" é o relógio da aplicação da #75 (`app.core.auth._agora`, em UTC),
o mesmo das sessões; chamado pelo módulo, para os testes congelarem um só.

Limites por plano (issue #133, ADR-019): `limite_da_conta` diz quantas
empresas ou quantos usuários a conta pode ter, pelas variáveis
`LIMITE_<RECURSO>_<PLANO>` (app/core/config.py). Vale o plano escolhido, com
assinatura ativa ou não; sem plano, os limites do essencial. Sem valor
configurado, `None`: não há limite. `chegou_ao_limite` é a regra do 402: a
conta no limite não adiciona mais, e quem já passou dele (limite ligado ou
reduzido depois) mantém tudo, só não cresce. Contar o que a conta usa precisa
de banco e fica em app/services/plano.py.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.core.config import Settings

PLANOS = ("essencial", "padrao", "avancado")
Plano = Literal["essencial", "padrao", "avancado"]
DURACAO_TESTE = timedelta(days=14)

Situacao = Literal["sem_plano", "teste", "teste_encerrado", "ativo"]

Recurso = Literal["empresas", "usuarios"]
# Sem plano escolhido, a conta tem os limites do plano de entrada.
PLANO_DOS_LIMITES_SEM_PLANO: Plano = "essencial"


class _ComPlano(Protocol):
    plano: str | None
    teste_termina_em: datetime | None
    assinatura_ativa: bool


def agora() -> datetime:
    # Import aqui: app.core.auth importa os models, e o model de conta
    # importa `PLANOS` deste módulo.
    from app.core import auth

    return auth._agora()


def limite_da_conta(
    conta: _ComPlano, recurso: Recurso, configuracao: "Settings | None" = None
) -> int | None:
    """Quantas empresas ou quantos usuários a conta pode ter; `None` é sem limite."""
    if configuracao is None:
        from app.core.config import settings as configuracao
    plano = conta.plano or PLANO_DOS_LIMITES_SEM_PLANO
    return getattr(configuracao, f"limite_{recurso}_{plano}")


def chegou_ao_limite(usados: int, maximo: int | None) -> bool:
    """A conta não pode adicionar mais um: está no limite ou acima dele."""
    return maximo is not None and usados >= maximo


def api_publica_liberada(conta: _ComPlano) -> bool:
    """A conta pode criar e usar chaves da API pública (issue #135, ADR-021)?
    No MVP, todos os planos, inclusive no teste: devolve sempre `True`. É o
    ponto único da regra para quando a API entrar nos preços."""
    return True


def mcp_liberado(conta: _ComPlano) -> bool:
    """A conta pode conectar o ChatGPT ou o Claude pelo MCP (issue #136,
    ADR-022)? No MVP, todos os planos: devolve sempre `True`, como
    `api_publica_liberada`. É o ponto único da regra."""
    return True


def situacao_da_conta(conta: _ComPlano, instante: datetime | None = None) -> Situacao:
    if conta.assinatura_ativa:
        return "ativo"
    if conta.plano is None or conta.teste_termina_em is None:
        return "sem_plano"
    if (instante or agora()) < conta.teste_termina_em:
        return "teste"
    return "teste_encerrado"
