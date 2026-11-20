"""Saída dos logs da aplicação (issue #102).

Os módulos de `app/` logam com `logging.getLogger(__name__)`, mas nada
configurava esses loggers: o uvicorn só configura os dele (`uvicorn`,
`uvicorn.error`, `uvicorn.access`), e o logger raiz fica sem handler. O Python
então descartava todo INFO e só deixava passar WARNING e acima, sem data nem
nome do módulo. Os `logger.info` da recuperação de senha, do e-mail e da IA
nunca chegavam ao log do Railway.

Aqui o logger `app` ganha um handler próprio para stderr, com o nível de
LOG_LEVEL. `propagate=False` para a linha não sair duas vezes se alguém
configurar o logger raiz; os loggers do uvicorn ficam como estão.

O log de acesso do uvicorn (`uvicorn.access`) escreve o caminho com a query
string, e a busca do painel (`GET /admin/contas?busca=...`) leva e-mail, CNPJ
ou nome (issue #234). `SemQueryNoAcesso` tira a query string do caminho de
TODA requisição, mantendo cliente, método, caminho, versão e status. Fica no
logger, não no handler: o uvicorn recria os handlers dele, mas o filtro vale
para o logger inteiro. Sem `--no-access-log`, que perderia o log de acesso.

Regra da issue #34, que vale para todo log de `app/`: nunca e-mail, nome,
razão social, CNPJ, descrição de lançamento, token ou link. Logue
identificadores (UUID), motivos, códigos de status e nomes de classe de
exceção, não a mensagem da exceção, que pode carregar o dado. Para uma
exceção, `resumo_do_erro(exc)`. `tests/test_logs_sem_dado_pessoal.py` confere
a regra em todo log de `app/`.

O erro que nenhuma rota trata chega ao uvicorn, que o registra no
`uvicorn.error` com o traceback inteiro (o Starlette relança a exceção mesmo
depois de um `exception_handler`). A mensagem de um erro de banco traz o dado:
o DETAIL do Postgres ("Key (email)=(...) already exists", "Failing row
contains (...)") e, sem `hide_parameters` no engine, os parâmetros da
consulta. `SemDetalheNoErro` troca esse registro por `erro_nao_tratado
classe=... causa=... sqlstate=... onde=arquivo:linha em função`, sem a
mensagem e sem o traceback. O `onde` é a última linha do projeto no traceback,
o que basta para achar o erro; o resto se reproduz localmente. Fica no logger,
como o filtro do acesso.
"""

import logging
import sys
import traceback
from pathlib import Path

from app.core.config import settings

FORMATO = "%(asctime)s %(levelname)s %(name)s %(message)s"
NOME_DO_HANDLER = "ledgr-stderr"
LOGGER_DE_ACESSO = "uvicorn.access"
LOGGER_DE_ERRO = "uvicorn.error"
RAIZ_DO_PROJETO = Path(__file__).resolve().parents[2]
# Posição do caminho nos args do uvicorn.access (uvicorn 0.54, h11 e httptools):
# '%s - "%s %s HTTP/%s" %d' com (cliente, método, caminho com query, versão, status).
POSICAO_DO_CAMINHO = 2


class SemQueryNoAcesso(logging.Filter):
    """Tira a query string do caminho no log de acesso do uvicorn (issue #234).

    Nunca recusa o registro, nunca levanta: com args em outro formato (outra
    versão do uvicorn, outro uso do logger), deixa a linha como veio."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) > POSICAO_DO_CAMINHO
            and isinstance(args[POSICAO_DO_CAMINHO], str)
        ):
            caminho = args[POSICAO_DO_CAMINHO].partition("?")[0]
            record.args = (*args[:POSICAO_DO_CAMINHO], caminho, *args[POSICAO_DO_CAMINHO + 1 :])
        return True


def _causa(exc: BaseException) -> BaseException | None:
    if exc.__cause__ is not None:
        return exc.__cause__
    return None if exc.__suppress_context__ else exc.__context__


def _sqlstate(exc: BaseException) -> str | None:
    """O SQLSTATE do erro do banco (ex.: 23505, 22012): no erro do psycopg, ou
    no `orig` do erro do SQLAlchemy que o embrulha, subindo pelas causas."""
    atual: BaseException | None = exc
    for _ in range(5):
        if atual is None:
            return None
        for candidato in (atual, getattr(atual, "orig", None)):
            sqlstate = getattr(candidato, "sqlstate", None)
            if isinstance(sqlstate, str):
                return sqlstate
        atual = _causa(atual)
    return None


def resumo_do_erro(exc: BaseException) -> str:
    """Classe, classe da causa e SQLSTATE do erro, sem a mensagem (regra da
    issue #34): `classe=IntegrityError causa=UniqueViolation sqlstate=23505`."""
    causa = _causa(exc)
    return (
        f"classe={type(exc).__name__} "
        f"causa={type(causa).__name__ if causa is not None else None} "
        f"sqlstate={_sqlstate(exc)}"
    )


def _e_do_projeto(arquivo: str) -> bool:
    caminho = Path(arquivo).resolve()
    return caminho.is_relative_to(RAIZ_DO_PROJETO) and "site-packages" not in caminho.parts


def _onde(rastro) -> str:
    """A última linha do projeto no traceback, sem as das bibliotecas."""
    ultima = None
    for quadro, linha in traceback.walk_tb(rastro):
        if _e_do_projeto(quadro.f_code.co_filename):
            ultima = (quadro.f_code.co_filename, linha, quadro.f_code.co_name)
    if ultima is None:
        return "desconhecido"
    arquivo, linha, funcao = ultima
    return f"{Path(arquivo).resolve().relative_to(RAIZ_DO_PROJETO).as_posix()}:{linha} em {funcao}"


class SemDetalheNoErro(logging.Filter):
    """Troca o registro de uma exceção pelo resumo dela, sem a mensagem e sem o
    traceback (issue #34; ver docstring do módulo).

    Nunca recusa o registro, nunca levanta: registro sem exceção passa como
    veio, e se o resumo falhar sai só a classe."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not (isinstance(exc_info, tuple) and isinstance(exc_info[1], BaseException)):
            return True
        exc = exc_info[1]
        try:
            record.msg, record.args = (
                "erro_nao_tratado %s onde=%s",
                (resumo_do_erro(exc), _onde(exc_info[2])),
            )
        except Exception:  # noqa: BLE001 - o filtro nunca derruba o log
            record.msg, record.args = "erro_nao_tratado classe=%s", (type(exc).__name__,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configurar_logs(nivel: str | None = None) -> None:
    """Liga a saída do logger `app` e os filtros do `uvicorn.access` e do
    `uvicorn.error`. Pode ser chamada mais de uma vez: troca o handler e não
    soma um segundo filtro, então nenhuma linha sai repetida nem processada
    duas vezes."""
    logger = logging.getLogger("app")
    for handler in list(logger.handlers):
        if handler.get_name() == NOME_DO_HANDLER:
            logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(NOME_DO_HANDLER)
    handler.setFormatter(logging.Formatter(FORMATO))
    logger.addHandler(handler)
    logger.setLevel(nivel or settings.log_level)
    logger.propagate = False

    acesso = logging.getLogger(LOGGER_DE_ACESSO)
    if not any(isinstance(filtro, SemQueryNoAcesso) for filtro in acesso.filters):
        acesso.addFilter(SemQueryNoAcesso())

    erro = logging.getLogger(LOGGER_DE_ERRO)
    if not any(isinstance(filtro, SemDetalheNoErro) for filtro in erro.filters):
        erro.addFilter(SemDetalheNoErro())
