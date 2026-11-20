"""Issue #186: `intervalo_da_competencia` (app/services/travas.py) é a forma em
SQL de `competencia_do_extrato`. Estes testes garantem que as duas regras
coincidem, dia a dia, incluindo virada de mês e de ano e ano bissexto."""

from datetime import date, timedelta

import pytest

from app.models import Extrato
from app.services.travas import competencia_do_extrato, intervalo_da_competencia


@pytest.mark.parametrize(
    ("competencia", "inicio", "fim"),
    [
        ("2026-09", date(2026, 9, 1), date(2026, 10, 1)),
        ("2026-02", date(2026, 2, 1), date(2026, 3, 1)),
        ("2028-02", date(2028, 2, 1), date(2028, 3, 1)),
        ("2026-12", date(2026, 12, 1), date(2027, 1, 1)),
        ("2026-01", date(2026, 1, 1), date(2026, 2, 1)),
    ],
)
def test_intervalo_da_competencia(competencia, inicio, fim):
    assert intervalo_da_competencia(competencia) == (inicio, fim)


def test_intervalo_coincide_com_competencia_do_extrato_dia_a_dia():
    dia = date(2027, 12, 25)
    while dia <= date(2029, 3, 5):
        competencia = dia.strftime("%Y-%m")
        extrato = Extrato(periodo_inicio=dia)
        for candidata in {competencia, (dia + timedelta(days=31)).strftime("%Y-%m")}:
            inicio, fim = intervalo_da_competencia(candidata)
            dentro = inicio <= dia < fim
            assert dentro == (competencia_do_extrato(extrato) == candidata), (dia, candidata)
        dia += timedelta(days=1)


def test_extrato_sem_periodo_fica_fora_de_qualquer_competencia():
    assert competencia_do_extrato(Extrato(periodo_inicio=None)) is None
