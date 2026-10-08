"""Guarda de banco local (issues #35 e #65): `exigir_banco_local`.

O `.env` da máquina de desenvolvimento aponta DATABASE_URL pro Postgres do
Railway, e três lugares dependem desta função pra não falar com ele por
acidente: o conftest (que encerra a suíte), o `migrations/env.py` (que recusa
migrar banco remoto sem `LEDGR_MIGRAR_BANCO_REMOTO=1`) e o
`scripts/ensaio_restore.py`. Sendo a única coisa entre um comando local e
produção, ela é testada sozinha, sem banco: é pura, só olha a URL.
"""

import pytest

from app.core.banco_local import HOSTS_LOCAIS, BancoNaoLocal, exigir_banco_local

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


def test_hosts_locais_sao_exatamente_os_tres_jeitos_de_escrever_esta_maquina():
    """Fixar o conjunto: acrescentar host aqui amplia o que a guarda libera em
    três lugares de uma vez (conftest, migrations/env.py, ensaio de restore)."""
    assert HOSTS_LOCAIS == {"localhost", "127.0.0.1", "::1"}


def test_recusa_url_com_host_remoto_mesmo_com_porta_local():
    """Porta de túnel local não torna o destino local: quem decide é o host."""
    with pytest.raises(BancoNaoLocal):
        exigir_banco_local("postgresql+psycopg://u:s@exemplo.invalid:55432/ensaio")
