"""Validação HTTP da competência (AAAA-MM), compartilhada pelas rotas que a
recebem por query param (`GET /fechamentos`, `GET /execucoes`).

A regra de formato mora em `app.services.fechamentos.competencia_valida`; aqui
só fica o 422 com o texto único, pra todas as rotas responderem igual.
"""

from datetime import date

from fastapi import HTTPException
from fastapi import status as http_status

from app.services.fechamentos import competencia_valida
from app.services.travas import intervalo_da_competencia

_TEXTO_COMPETENCIA_INVALIDA = "A competência deve estar no formato AAAA-MM, com mês de 01 a 12."


def _competencia_invalida() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=_TEXTO_COMPETENCIA_INVALIDA,
    )


def validar_competencia(competencia: str) -> None:
    if not competencia_valida(competencia):
        raise _competencia_invalida()


def intervalo_da_competencia_ou_422(competencia: str) -> tuple[date, date]:
    """Valida a competência e devolve o intervalo de datas do mês (início e fim
    exclusivo), para os filtros em SQL (`GET /execucoes`).

    O formato AAAA-MM aceita `0000-01` e `9999-12`, mas o intervalo precisa de
    `date(0, 1, 1)` e `date(10000, 1, 1)`, fora do que `datetime.date` representa.
    Esses dois casos dão o mesmo 422 do formato inválido, em vez de 500.
    `/fechamentos` não usa este helper e segue aceitando esses valores."""
    validar_competencia(competencia)
    try:
        return intervalo_da_competencia(competencia)
    except ValueError:
        raise _competencia_invalida() from None
