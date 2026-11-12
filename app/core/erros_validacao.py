"""Handler do 422 de validação (issue #184).

O handler padrão do FastAPI devolve o erro do Pydantic inteiro, com o campo
`input`: o valor que a pessoa enviou. Para `cidade` e `uf` (dado da conta, que
não vai a log nem a corpo de erro), este handler tira o `input` do erro. Também
o tira do erro sobre o corpo inteiro (`loc` só `["body"]`), que carregaria os
dois campos junto do resto. Tudo mais fica como o padrão: mesmo status, mesmo
formato (`{"detail": [...]}`), com `type`, `loc` e `msg`.
"""

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response

CAMPOS_SEM_VALOR = frozenset({"cidade", "uf"})


def _esconde_o_valor(erro: dict) -> bool:
    loc = tuple(erro.get("loc", ()))
    return loc == ("body",) or any(parte in CAMPOS_SEM_VALOR for parte in loc)


def sem_valor_enviado(erros: list[dict]) -> list[dict]:
    return [
        (
            {chave: valor for chave, valor in erro.items() if chave != "input"}
            if _esconde_o_valor(erro)
            else erro
        )
        for erro in erros
    ]


async def tratar_validacao(request: Request, exc: RequestValidationError) -> Response:
    return await request_validation_exception_handler(
        request, RequestValidationError(sem_valor_enviado(exc.errors()))
    )
