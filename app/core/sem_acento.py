"""Comparação sem acento e sem maiúsculas, sem a extensão `unaccent`.

O mesmo mapa normaliza a coluna no banco (`translate`) e o texto buscado no
Python, então os dois lados comparam igual qualquer que seja o locale do
Postgres. Usado na busca de empresas (issue #157) e na busca do topo do app
(issue #220). `translate` é IMMUTABLE no Postgres, então a expressão de
`sem_acento_sql` pode ter índice (o trigram da busca, issue #220).
"""

from sqlalchemy import func

# Acentos do português, maiúsculos e minúsculos, e a letra sem acento.
COM_ACENTO = "ÁÀÂÃÄáàâãäÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÕÖóòôõöÚÙÛÜúùûüÇçÑñ"
SEM_ACENTO = "aaaaaaaaaaeeeeeeeeiiiiiiiioooooooooouuuuuuuuccnn"
_SEM_ACENTO_PYTHON = str.maketrans(COM_ACENTO, SEM_ACENTO)


def sem_acento_sql(coluna):
    """`lower(translate(coluna, COM_ACENTO, SEM_ACENTO))`."""
    return func.lower(func.translate(coluna, COM_ACENTO, SEM_ACENTO))


def sem_acento(texto: str) -> str:
    """O texto como `sem_acento_sql` o deixa no banco."""
    return texto.translate(_SEM_ACENTO_PYTHON).lower()
