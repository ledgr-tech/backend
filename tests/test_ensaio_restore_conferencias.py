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


# --- issue #215: colunas de remoção em `empresas` ---------------------------------------

REMOVIDAS = "`empresas.removida_em` existe e nenhuma empresa nasceu removida (issue #215)"


def _removidas(antes: Inventario, depois: Inventario) -> bool:
    (conferencia,) = [c for c in conferir(antes, depois, ["head"]) if c.nome == REMOVIDAS]
    return conferencia.passou


def _com_removidas(removidas: int | None, **contagens: int) -> Inventario:
    inventario = _inventario(**contagens)
    inventario.empresas_removidas = removidas
    return inventario


def test_dump_anterior_a_migracao_ganha_a_coluna_sem_empresa_removida():
    assert _removidas(_com_removidas(None, empresas=3), _com_removidas(0, empresas=3)) is True


def test_dump_posterior_a_migracao_mantem_as_removidas():
    assert _removidas(_com_removidas(2, empresas=5), _com_removidas(2, empresas=5)) is True


@pytest.mark.parametrize(("antes", "depois"), [(None, 1), (0, 3), (2, 1), (1, None), (None, None)])
def test_empresa_que_nasce_removida_ou_coluna_ausente_reprova(antes, depois):
    assert (
        _removidas(_com_removidas(antes, empresas=5), _com_removidas(depois, empresas=5)) is False
    )
