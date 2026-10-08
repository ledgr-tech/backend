"""Guarda que recusa uma URL de banco que não aponte pra esta máquina (issue #65).

Existe porque o `.env` do repositório aponta DATABASE_URL pro Postgres do
Railway: é o banco compartilhado de desenvolvimento, e em produção é o banco de
verdade. Qualquer coisa que leia esse `.env` sem pensar — um `pytest`, um
`alembic upgrade head` rodado na pasta errada — fala com produção. Três lugares
chamam esta função pra que isso não aconteça:

- `tests/conftest.py`, que encerra a suíte inteira se DATABASE_URL não for local
  (teste grava e apaga linha; contra produção é perda de dado);
- `migrations/env.py` no modo online, que recusa migrar banco remoto a não ser
  que `LEDGR_MIGRAR_BANCO_REMOTO=1` esteja definida (é o que o pre-deploy do
  Railway define, de propósito);
- `scripts/ensaio_restore.py` (issue #35), antes de cada comando que recebe
  DATABASE_URL.

É função pura, sem I/O e sem dependência de nada do projeto: dá pra chamar antes
de abrir conexão, antes de subprocesso, e testar sem banco.
"""

from urllib.parse import parse_qs, urlsplit

# Os três jeitos de escrever "esta máquina". Qualquer outra coisa é recusada.
HOSTS_LOCAIS = frozenset({"localhost", "127.0.0.1", "::1"})


class BancoNaoLocal(RuntimeError):
    """A URL de conexão aponta pra fora desta máquina."""


def exigir_banco_local(url: str) -> str:
    """Devolve `url` se ela aponta pro Postgres local; levanta `BancoNaoLocal` se não.

    Recusa, nesta ordem: URL inválida, host ausente (`postgresql:///ensaio` —
    aí o libpq cai no default do ambiente e o destino não está escrito na URL),
    host fora de {localhost, 127.0.0.1, ::1} — inclusive vizinho de nome como
    `127.0.0.1.exemplo.com`, que casaria num teste de prefixo — e o parâmetro
    `host=` na query, que no libpq tem precedência sobre o host da URL e
    driblaria a checagem acima.
    """
    try:
        partes = urlsplit(url)
        host, _porta = partes.hostname, partes.port
    except ValueError as erro:
        raise BancoNaoLocal(f"URL de conexão inválida ({erro}): {url!r}") from erro
    if not host:
        raise BancoNaoLocal(f"URL de conexão sem host explícito, recusada: {url!r}")
    if host not in HOSTS_LOCAIS:
        raise BancoNaoLocal(
            f"URL de conexão aponta pro host {host!r}, que não é local. "
            f"Só {sorted(HOSTS_LOCAIS)} é aceito aqui."
        )
    if "host" in parse_qs(partes.query):
        raise BancoNaoLocal(f"URL de conexão traz `host=` na query, recusada: {url!r}")
    return url
