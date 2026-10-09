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


def configurar_logs(nivel: str | None = None) -> None:
    """Liga a saída do logger `app`. Pode ser chamada mais de uma vez: troca o
    handler em vez de somar outro, então nenhuma linha sai repetida."""
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
