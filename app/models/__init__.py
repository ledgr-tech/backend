"""Registro central dos models, pra Alembic (autogenerate) enxergar todos.

Sempre que um model novo for criado, importar aqui também.
"""

from app.models.base import Base
from app.models.configuracao import Configuracao
from app.models.empresa import Empresa
from app.models.extrato import Extrato
from app.models.lancamento import Lancamento
from app.models.linha_invalida import LinhaInvalida
from app.models.usuario import Usuario

__all__ = ["Base", "Configuracao", "Empresa", "Extrato", "Lancamento", "LinhaInvalida", "Usuario"]
