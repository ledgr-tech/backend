"""Validação HTTP da competência (AAAA-MM), compartilhada pelas rotas que a
recebem por query param (`GET /fechamentos`, `GET /execucoes`).

A regra de formato mora em `app.services.fechamentos.competencia_valida`; aqui
só fica o 422 com o texto único, pra todas as rotas responderem igual.
"""

from fastapi import HTTPException
from fastapi import status as http_status

from app.services.fechamentos import competencia_valida


def validar_competencia(competencia: str) -> None:
    if not competencia_valida(competencia):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A competência deve estar no formato AAAA-MM, com mês de 01 a 12.",
        )
