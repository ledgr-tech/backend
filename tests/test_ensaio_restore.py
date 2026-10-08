"""Guarda do ensaio de restore (issue #35): `exigir_banco_local`.

`scripts/ensaio_restore.py` roda `alembic upgrade head` como subprocesso, e o
`.env` da máquina de desenvolvimento aponta DATABASE_URL pro Postgres do
Railway. A única coisa entre o ensaio e uma migração em produção é esta função,
então ela é testada sozinha, sem banco: é pura, só olha a URL.
"""

import pytest

from scripts.ensaio_restore import BancoNaoLocal, exigir_banco_local

URLS_LOCAIS = [
    "postgresql+psycopg://postgres:senha@127.0.0.1:55432/ensaio",
    "postgresql+psycopg://postgres:senha@localhost:55432/ensaio",
    "postgresql+psycopg://postgres:senha@[::1]:55432/ensaio",
    # Sem porta e sem credencial continuam locais.
    "postgresql://localhost/ensaio",
    # urlsplit normaliza a caixa do host, então LOCALHOST é o mesmo host.
    "postgresql+psycopg://postgres:senha@LOCALHOST:55432/ensaio",
]

URLS_RECUSADAS = [
    # O caso que motiva a guarda: o Railway do .env.
    "postgresql+psycopg://postgres:senha@shuttle.proxy.rlwy.net:15513/railway",
    # Vizinho de nome: passaria numa checagem por prefixo.
    "postgresql+psycopg://postgres:senha@127.0.0.1.exemplo.com:55432/ensaio",
    "postgresql+psycopg://postgres:senha@localhost.exemplo.com:55432/ensaio",
    # Outro loopback da faixa 127/8: fora da lista, então recusado.
    "postgresql+psycopg://postgres:senha@127.0.0.2:55432/ensaio",
    # Host ausente: o libpq cairia no default (socket local ou PGHOST do
    # ambiente), e aí o destino não está escrito na URL.
    "postgresql+psycopg:///ensaio",
    "postgresql+psycopg://postgres:senha@/ensaio",
    # `host=` na query tem precedência sobre o host da URL no libpq.
    "postgresql+psycopg://postgres:senha@127.0.0.1:55432/ensaio?host=shuttle.proxy.rlwy.net",
    # Porta inválida: urlsplit levanta ValueError ao ler `.port`.
    "postgresql+psycopg://postgres:senha@127.0.0.1:porta/ensaio",
    "",
]


@pytest.mark.parametrize("url", URLS_LOCAIS)
def test_aceita_banco_local_e_devolve_a_url(url):
    assert exigir_banco_local(url) == url


@pytest.mark.parametrize("url", URLS_RECUSADAS)
def test_recusa_qualquer_coisa_que_nao_seja_banco_local(url):
    with pytest.raises(BancoNaoLocal):
        exigir_banco_local(url)


def test_mensagem_de_recusa_nomeia_o_host_pra_quem_lê_o_log():
    with pytest.raises(BancoNaoLocal, match="shuttle.proxy.rlwy.net"):
        exigir_banco_local(
            "postgresql+psycopg://postgres:senha@shuttle.proxy.rlwy.net:15513/railway"
        )
