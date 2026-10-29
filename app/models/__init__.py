"""Registro central dos models, pra Alembic (autogenerate) enxergar todos.

Sempre que um model novo for criado, importar aqui também.
"""

from app.models.admin import AcessoAdmin, EventoConta, OperadorLedgr
from app.models.assistente import PerguntaSemResposta, UsoAssistente
from app.models.base import Base
from app.models.chave_api import ChaveApi
from app.models.conciliacao import Conciliacao
from app.models.configuracao import Configuracao
from app.models.conta import Conta
from app.models.convite import Convite
from app.models.decisao_linha import DecisaoLinha
from app.models.empresa import Empresa
from app.models.empresa_favorita import EmpresaFavorita
from app.models.execucao_conciliacao import ExecucaoConciliacao
from app.models.explicacao_divergencia import ExplicacaoDivergencia
from app.models.extrato import Extrato
from app.models.fechamento import Fechamento
from app.models.lancamento import Lancamento
from app.models.linha_invalida import LinhaInvalida
from app.models.link_relatorio import LinkRelatorio
from app.models.oauth import (
    ClienteOAuth,
    CodigoOAuth,
    ConexaoMcp,
    PedidoAutorizacaoOAuth,
    TokenOAuth,
)
from app.models.registro_exclusao import RegistroExclusao
from app.models.token_email import TokenEmail
from app.models.usuario import Usuario

__all__ = [
    "AcessoAdmin",
    "Base",
    "ChaveApi",
    "ClienteOAuth",
    "CodigoOAuth",
    "Conciliacao",
    "ConexaoMcp",
    "Configuracao",
    "Conta",
    "Convite",
    "DecisaoLinha",
    "Empresa",
    "EmpresaFavorita",
    "EventoConta",
    "ExecucaoConciliacao",
    "ExplicacaoDivergencia",
    "Extrato",
    "Fechamento",
    "Lancamento",
    "LinhaInvalida",
    "LinkRelatorio",
    "OperadorLedgr",
    "PedidoAutorizacaoOAuth",
    "PerguntaSemResposta",
    "RegistroExclusao",
    "TokenEmail",
    "TokenOAuth",
    "UsoAssistente",
    "Usuario",
]
