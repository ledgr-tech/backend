"""Registro central dos models, pra Alembic (autogenerate) enxergar todos.

Sempre que um model novo for criado, importar aqui também.
"""

from app.models.base import Base
from app.models.conciliacao import Conciliacao
from app.models.configuracao import Configuracao
from app.models.empresa import Empresa
from app.models.execucao_conciliacao import ExecucaoConciliacao
from app.models.explicacao_divergencia import ExplicacaoDivergencia
from app.models.extrato import Extrato
from app.models.lancamento import Lancamento
from app.models.linha_invalida import LinhaInvalida
from app.models.usuario import Usuario

__all__ = [
    "Base",
    "Conciliacao",
    "Configuracao",
    "Empresa",
    "ExecucaoConciliacao",
    "ExplicacaoDivergencia",
    "Extrato",
    "Lancamento",
    "LinhaInvalida",
    "Usuario",
]
