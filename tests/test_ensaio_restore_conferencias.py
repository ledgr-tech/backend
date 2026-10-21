"""Conferências do ensaio de restore (scripts/ensaio_restore.py) diante da
migração da issue #133: `contas` é a única tabela que a migração cria já
preenchida, com uma conta por empresa. Só a comparação dos retratos, sem
Docker nem banco.
"""

import pytest

from scripts.ensaio_restore import Inventario, conferir

CONFERENCIA = "tabelas criadas pela migração estão vazias (`contas`: uma por empresa)"


def _inventario(**contagens: int) -> Inventario:
    return Inventario(
        versoes_alembic=["head"],
        contagens=contagens,
        distribuicao_tolerancia=[],
        column_default="0",
        acima_do_maximo=0,
        constraint_presente=True,
    )


def _resultado(antes: Inventario, depois: Inventario) -> bool:
    (conferencia,) = [c for c in conferir(antes, depois, ["head"]) if c.nome == CONFERENCIA]
    return conferencia.passou


def test_contas_com_uma_linha_por_empresa_passa():
    antes = _inventario(empresas=3, usuarios=5)
    depois = _inventario(empresas=3, usuarios=5, contas=3)

    assert _resultado(antes, depois) is True


@pytest.mark.parametrize("contas", [0, 2, 4])
def test_contas_com_outra_quantidade_falha(contas):
    antes = _inventario(empresas=3)
    depois = _inventario(empresas=3, contas=contas)

    assert _resultado(antes, depois) is False


def test_outra_tabela_nova_continua_tendo_de_nascer_vazia():
    antes = _inventario(empresas=1)
    depois = _inventario(empresas=1, contas=1, tabela_nova=1)

    assert _resultado(antes, depois) is False
