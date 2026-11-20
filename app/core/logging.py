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
descrição de lançamento, token ou link. Logue identificadores (UUID), motivos,
códigos de status e nomes de classe de exceção, não a mensagem da exceção,
que pode carregar o dado.
"""

import logging
import sys

from app.core.config import settings

FORMATO = "%(asctime)s %(levelname)s %(name)s %(message)s"
NOME_DO_HANDLER = "ledgr-stderr"
LOGGER_DE_ACESSO = "uvicorn.access"
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


def configurar_logs(nivel: str | None = None) -> None:
    """Liga a saída do logger `app` e o filtro do `uvicorn.access`. Pode ser
    chamada mais de uma vez: troca o handler e não soma um segundo filtro, então
    nenhuma linha sai repetida nem processada duas vezes."""
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
