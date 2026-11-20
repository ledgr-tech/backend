"""Exclusão dos dados de uma conta inteira ou de uma empresa cliente (issues #68,
#133 e #215, ADR-017 e ADR-019).

É a lógica que scripts/excluir_empresa.py (a conta inteira, a pedido) e
scripts/apagar_empresas_removidas.py (uma empresa removida há mais de 30 dias)
dividem. O que sai de cada tabela está em `ETAPAS`, uma vez só: o `Escopo` diz
se a condição vale para a conta toda ou para uma empresa dela.

Nada aqui faz commit nem fala com o R2: quem chama decide a transação e apaga
os arquivos originais antes dela (ver `prefixo_da_conta` e
`prefixo_da_empresa` em app/services/arquivo_original.py).

As consultas pedem `incluir_removidas`: a empresa removida é justamente o que a
limpeza apaga, e o filtro de sessão de app/models/empresa.py a esconderia.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    AssinaturaStripe,
    Base,
    ChaveApi,
    CodigoOAuth,
    Conciliacao,
    ConexaoMcp,
    Configuracao,
    Conta,
    Convite,
    CupomEnviado,
    DecisaoLinha,
    Empresa,
    EmpresaFavorita,
    EventoConta,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    LinkRelatorio,
    OperadorLedgr,
    PagamentoStripe,
    PedidoAutorizacaoOAuth,
    TokenEmail,
    TokenOAuth,
    UsoAssistente,
    Usuario,
)
from app.models.empresa import INCLUIR_REMOVIDAS

COM_REMOVIDAS = {INCLUIR_REMOVIDAS: True}


@dataclass(frozen=True)
class Escopo:
    """O que se apaga: a conta inteira (`empresa_id` nulo) ou uma empresa dela."""

    conta_id: uuid.UUID
    empresa_id: uuid.UUID | None = None

    @property
    def da_conta_inteira(self) -> bool:
        return self.empresa_id is None

    def empresas(self):
        """Os ids das empresas do escopo, como subconsulta."""
        consulta = select(Empresa.id).where(Empresa.conta_id == self.conta_id)
        if self.empresa_id is not None:
            consulta = consulta.where(Empresa.id == self.empresa_id)
        return consulta.execution_options(**COM_REMOVIDAS)

    def extratos(self):
        return select(Extrato.id).where(Extrato.empresa_id.in_(self.empresas()))

    def usuarios(self):
        return select(Usuario.id).where(Usuario.conta_id == self.conta_id)

    def conexoes(self):
        """As conexões MCP do escopo: a conta toda, ou as da empresa."""
        consulta = select(ConexaoMcp.id).where(ConexaoMcp.conta_id == self.conta_id)
        if self.empresa_id is not None:
            consulta = consulta.where(ConexaoMcp.empresa_id == self.empresa_id)
        return consulta


Condicao = Callable[[Escopo], ColumnElement[bool] | None]


def _das_empresas(coluna_empresa_id) -> Condicao:
    """Tabelas por `empresa_id`: as linhas das empresas do escopo."""
    return lambda escopo: coluna_empresa_id.in_(escopo.empresas())


def _so_da_conta(condicao: Condicao) -> Condicao:
    """Tabela que é da conta e não de uma empresa: só sai com a conta inteira."""
    return lambda escopo: condicao(escopo) if escopo.da_conta_inteira else None


# Ordem das FKs, conferida nos models: quem aponta para uma tabela sai antes
# dela. As tabelas por `empresa_id` saem pelas empresas do escopo;
# `linhas_invalidas` e `tokens_email` saem pelo extrato e pelo usuário;
# `tokens_oauth` e `codigos_oauth`, pela conexão MCP (issue #136);
# `empresas_favoritas`, pela empresa (issue #157), antes de usuários e
# empresas; `links_relatorio` (issue #165), antes dos fechamentos;
# `cupons_enviados` (issue #169), `eventos_conta` e `operadores_ledgr` (issue
# #156), antes dos usuários; `empresas` aponta para `usuarios` (quem removeu,
# issue #215), então sai antes deles;
# `assinaturas_stripe` e `pagamentos_stripe` (issues #166 e #167), antes da
# conta. Apagar a conta não cancela nada na Stripe: o script recusa a conta com
# assinatura em andamento. O
# `acessos_admin` fica: não tem FK nem `conta_id`, é o registro de quem do
# Ledgr consultou a conta. Os `clientes_oauth` ficam: não são de nenhuma conta.
#
# Escopo de uma empresa (issue #215): sai o que é dela. A condição devolve
# `None` para o que é da conta (convites, usuários, plano, o pedido de
# autorização OAuth), que a empresa removida não leva embora. A chave de API
# da conta toda também fica; só as chaves escopadas àquela empresa saem.
# tests/test_excluir_empresa.py confere a ordem e que nenhuma tabela ligada à
# conta ou às empresas dela ficou de fora.
ETAPAS: tuple[tuple[type[Base], Condicao], ...] = (
    (ExplicacaoDivergencia, _das_empresas(ExplicacaoDivergencia.empresa_id)),
    (DecisaoLinha, _das_empresas(DecisaoLinha.empresa_id)),
    (Conciliacao, _das_empresas(Conciliacao.empresa_id)),
    (ExecucaoConciliacao, _das_empresas(ExecucaoConciliacao.empresa_id)),
    (LinkRelatorio, _das_empresas(LinkRelatorio.empresa_id)),
    (Fechamento, _das_empresas(Fechamento.empresa_id)),
    (LinhaInvalida, lambda e: LinhaInvalida.extrato_id.in_(e.extratos())),
    (Lancamento, _das_empresas(Lancamento.empresa_id)),
    (Extrato, _das_empresas(Extrato.empresa_id)),
    (Convite, _so_da_conta(lambda e: Convite.conta_id == e.conta_id)),
    (
        ChaveApi,
        lambda e: (
            ChaveApi.conta_id == e.conta_id
            if e.da_conta_inteira
            else ChaveApi.empresa_id == e.empresa_id
        ),
    ),
    (TokenOAuth, lambda e: TokenOAuth.conexao_id.in_(e.conexoes())),
    (CodigoOAuth, lambda e: CodigoOAuth.conexao_id.in_(e.conexoes())),
    (ConexaoMcp, lambda e: ConexaoMcp.id.in_(e.conexoes())),
    (
        PedidoAutorizacaoOAuth,
        _so_da_conta(lambda e: PedidoAutorizacaoOAuth.conta_id == e.conta_id),
    ),
    (TokenEmail, _so_da_conta(lambda e: TokenEmail.usuario_id.in_(e.usuarios()))),
    (Configuracao, _das_empresas(Configuracao.empresa_id)),
    (EmpresaFavorita, _das_empresas(EmpresaFavorita.empresa_id)),
    (CupomEnviado, _so_da_conta(lambda e: CupomEnviado.conta_id == e.conta_id)),
    (EventoConta, _so_da_conta(lambda e: EventoConta.conta_id == e.conta_id)),
    (PagamentoStripe, _so_da_conta(lambda e: PagamentoStripe.conta_id == e.conta_id)),
    (AssinaturaStripe, _so_da_conta(lambda e: AssinaturaStripe.conta_id == e.conta_id)),
    (OperadorLedgr, _so_da_conta(lambda e: OperadorLedgr.usuario_id.in_(e.usuarios()))),
    (Empresa, lambda e: Empresa.id.in_(e.empresas())),
    (Usuario, _so_da_conta(lambda e: Usuario.conta_id == e.conta_id)),
    (UsoAssistente, _so_da_conta(lambda e: UsoAssistente.conta_id == e.conta_id)),
    (Conta, _so_da_conta(lambda e: Conta.id == e.conta_id)),
)


def tabelas_apagadas(etapas=ETAPAS) -> list[str]:
    return [model.__tablename__ for model, _ in etapas]


def contar_linhas(db: Session, escopo: Escopo, etapas=ETAPAS) -> dict[str, int]:
    """Quantas linhas de cada tabela são do escopo, na ordem de `etapas`. Nas
    tabelas que o escopo não apaga, zero."""
    contagens = {}
    for model, condicao in etapas:
        filtro = condicao(escopo)
        contagens[model.__tablename__] = (
            0
            if filtro is None
            else db.scalar(
                select(func.count())
                .select_from(model)
                .where(filtro)
                .execution_options(**COM_REMOVIDAS)
            )
        )
    return contagens


def apagar_linhas(db: Session, escopo: Escopo, etapas=ETAPAS) -> dict[str, int]:
    """Apaga, na ordem de `etapas`, as linhas do escopo, sem commit. Devolve
    quantas saíram de cada tabela, na mesma ordem.

    A linha da conta e as das empresas do escopo são travadas antes de tudo: as
    tabelas com `empresa_id` têm FK para a empresa, e usuários, convites e
    empresas, para a conta, então um upload ou cadastro simultâneo espera e
    depois falha pela FK, em vez de deixar linha órfã."""
    conta = db.scalar(select(Conta).where(Conta.id == escopo.conta_id).with_for_update())
    if conta is None:
        raise LookupError("conta não encontrada")
    db.execute(escopo.empresas().with_for_update())
    contagens = {}
    for model, condicao in etapas:
        filtro = condicao(escopo)
        if filtro is None:
            contagens[model.__tablename__] = 0
            continue
        resultado = db.execute(
            delete(model).where(filtro), execution_options={"synchronize_session": False}
        )
        contagens[model.__tablename__] = resultado.rowcount
    return contagens
