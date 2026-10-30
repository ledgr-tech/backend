"""Plano da conta e período de teste (issue #122): a regra, sem banco. Desde a
issue #133 (ADR-019), o plano é da conta, e não mais da empresa.

`PLANOS` é a lista única dos planos escolhíveis; o CHECK de `contas.plano`,
o corpo do `POST /empresa/plano` e o script de ativação saem dela. "Volume"
não está aqui: é contato comercial. Nenhum preço mora no backend.

`situacao_da_conta` é a única conta da situação, usada pelo `GET /me`, pelo
`POST /empresa/plano`, pelo `/admin` e pelo bloqueio do `POST /conciliacoes`,
para nunca divergirem. É calculada, nunca gravada. Desde as issues #166 e #167
(ADR-026), o cadastro começa o teste de 14 dias sem plano e sem cartão, e a
cobrança é a assinatura da Stripe (`assinaturas_stripe`, mantida só pelo
webhook). Na ordem:

- "ativo": `assinatura_ativa` (o piloto, marcado à mão por
  scripts/ativar_empresa.py ou pelo `/admin`) ou assinatura da Stripe `active`
  ou `trialing`;
- "pagamento_pendente": assinatura `past_due`; o acesso segue enquanto a Stripe
  tenta cobrar de novo;
- "assinatura_encerrada": assinatura `canceled`, `unpaid`, `incomplete_expired`
  ou `paused`; barra nova conciliação como o teste encerrado;
- sem assinatura viva (ou só `incomplete`, o primeiro pagamento ainda não
  confirmado): "sem_plano" sem `teste_termina_em`; "teste" antes dele;
  "teste_encerrado" de `teste_termina_em` em diante.

O cancelamento agendado (`cancelar_no_fim_do_periodo`) não muda nada: a
situação segue "ativo" até a Stripe encerrar a assinatura.

O "agora" é o relógio da aplicação da #75 (`app.core.auth._agora`, em UTC),
o mesmo das sessões; chamado pelo módulo, para os testes congelarem um só.

Limites por plano (issue #133, ADR-019): `limite_da_conta` diz quantas
empresas, quantos usuários ou quantas conciliações por mês a conta pode ter,
pelas variáveis `LIMITE_<RECURSO>_<PLANO>` (app/core/config.py). Vale o plano
efetivo (`plano_efetivo`), em qualquer situação: o da assinatura da Stripe
quando ela existe; senão o escolhido em `contas.plano`; senão os limites do
essencial. Sem valor configurado, `None`: não há limite. `chegou_ao_limite` é
a regra do 402: a conta no limite não adiciona mais, e quem já passou dele
(limite ligado ou reduzido depois) mantém tudo, só não cresce. Contar o que a
conta usa precisa de banco e fica em app/services/plano.py.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.core.config import Settings

PLANOS = ("essencial", "padrao", "avancado")
Plano = Literal["essencial", "padrao", "avancado"]
# Período de cobrança do preço (a `lookup_key` é "<plano>_<periodo>").
PERIODOS = ("mensal", "trimestral", "semestral", "anual")
Periodo = Literal["mensal", "trimestral", "semestral", "anual"]
DURACAO_TESTE = timedelta(days=14)

Situacao = Literal[
    "sem_plano",
    "teste",
    "teste_encerrado",
    "ativo",
    "pagamento_pendente",
    "assinatura_encerrada",
]

# Status da assinatura na Stripe (Subscription.status) -> situação da conta.
# `incomplete` fica de fora: o primeiro pagamento ainda não foi confirmado, e a
# conta segue pelo teste. `paused` não acontece (o checkout sempre coleta o
# cartão), mas, se vier, não dá acesso.
STATUS_ATIVOS = frozenset({"active", "trialing"})
STATUS_PAGAMENTO_PENDENTE = frozenset({"past_due"})
STATUS_ENCERRADOS = frozenset({"canceled", "unpaid", "incomplete_expired", "paused"})
# A conta já tem assinatura andando: novo checkout é 409, e a escolha de plano
# pelo `POST /empresa/plano` também.
STATUS_EM_ANDAMENTO = frozenset({"active", "trialing", "past_due", "incomplete"})

Recurso = Literal["empresas", "usuarios", "conciliacoes"]
# Sem plano escolhido, a conta tem os limites do plano de entrada.
PLANO_DOS_LIMITES_SEM_PLANO: Plano = "essencial"


class _ComAssinatura(Protocol):
    status: str
    plano: str


class _ComPlano(Protocol):
    plano: str | None
    teste_termina_em: datetime | None
    assinatura_ativa: bool
    assinatura_stripe: _ComAssinatura | None


def agora() -> datetime:
    # Import aqui: app.core.auth importa os models, e o model de conta
    # importa `PLANOS` deste módulo.
    from app.core import auth

    return auth._agora()


def plano_efetivo(conta: _ComPlano) -> Plano:
    """O plano que vale para limites e para o arquivo original: o da assinatura
    da Stripe quando ela existe (qualquer status); senão o escolhido em
    `contas.plano`; senão o de entrada."""
    assinatura = getattr(conta, "assinatura_stripe", None)
    if assinatura is not None:
        return assinatura.plano
    return conta.plano or PLANO_DOS_LIMITES_SEM_PLANO


def limite_da_conta(
    conta: _ComPlano, recurso: Recurso, configuracao: "Settings | None" = None
) -> int | None:
    """Quantas empresas, quantos usuários ou quantas conciliações por mês a conta
    pode ter; `None` é sem limite."""
    if configuracao is None:
        from app.core.config import settings as configuracao
    return getattr(configuracao, f"limite_{recurso}_{plano_efetivo(conta)}")


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


# Arquivo original do extrato (issue #139, ADR-023): só o plano mais caro.
PLANOS_QUE_GUARDAM_ARQUIVO: tuple[Plano, ...] = ("avancado",)


def guarda_arquivo_original(conta: _ComPlano) -> bool:
    """O plano da conta guarda o arquivo original de cada extrato enviado? Vale
    o plano efetivo, em qualquer situação, como nos limites. Só a regra do
    plano: a flag e a configuração do R2 quem confere é a rota."""
    return plano_efetivo(conta) in PLANOS_QUE_GUARDAM_ARQUIVO


def situacao_da_conta(conta: _ComPlano, instante: datetime | None = None) -> Situacao:
    if conta.assinatura_ativa:
        return "ativo"
    assinatura = getattr(conta, "assinatura_stripe", None)
    if assinatura is not None:
        if assinatura.status in STATUS_ATIVOS:
            return "ativo"
        if assinatura.status in STATUS_PAGAMENTO_PENDENTE:
            return "pagamento_pendente"
        if assinatura.status in STATUS_ENCERRADOS:
            return "assinatura_encerrada"
    if conta.teste_termina_em is None:
        return "sem_plano"
    if (instante or agora()) < conta.teste_termina_em:
        return "teste"
    return "teste_encerrado"
