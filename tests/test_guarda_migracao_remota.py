"""Guarda do `migrations/env.py` contra migrar banco remoto (issue #65).

Desde esta issue o deploy do Railway roda `alembic upgrade head` sozinho, num
pre-deploy. O efeito colateral é que ninguém mais precisa rodar alembic à mão
contra produção — e como o `.env` do repositório aponta DATABASE_URL pro
Railway, um `alembic upgrade head` rodado sem DATABASE_URL explícito migraria
produção calado. `migrations/env.py` recusa isso, a não ser que
`LEDGR_MIGRAR_BANCO_REMOTO=1` esteja definida (é o que o serviço do Railway
define, de propósito).

Os testes rodam o alembic como subprocesso, com o env montado na mão — é o único
jeito de exercitar o `env.py` de verdade, que lê `os.getenv` no momento em que
roda. A URL aponta pro host `exemplo.invalid`: `.invalid` é TLD reservado pela
RFC 2606 e nunca resolve, então "tentou conectar" aparece como erro de DNS no
stderr. É assim que o primeiro teste prova que a guarda barrou ANTES de abrir
conexão, e não depois.
"""

import os
import subprocess
import sys

import pytest

from app.core.banco_local import BancoNaoLocal

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL_REMOTA = "postgresql+psycopg://usuario:senha@exemplo.invalid:5432/railway"
VARIAVEL = "LEDGR_MIGRAR_BANCO_REMOTO"
# Marca de que o psycopg chegou a tentar resolver o host.
ERRO_DE_CONEXAO = "exemplo.invalid"


def _alembic_upgrade(**env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != VARIAVEL}
    env["DATABASE_URL"] = URL_REMOTA
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_sem_a_variavel_recusa_banco_remoto_antes_de_conectar():
    processo = _alembic_upgrade()
    saida = processo.stdout + processo.stderr

    assert processo.returncode != 0
    assert BancoNaoLocal.__name__ in saida
    assert "exemplo.invalid" in saida, "a mensagem tem de nomear o host recusado"
    assert VARIAVEL in saida, "a mensagem tem de dizer qual variável destrava"
    # Não chegou a abrir conexão: se tivesse, o psycopg falharia no DNS do
    # `.invalid` e o stderr traria o erro de resolução, não o da guarda.
    assert "failed to resolve host" not in saida
    assert "Name or service not known" not in saida
    assert "OperationalError" not in saida


def test_com_a_variavel_a_guarda_libera_e_o_alembic_segue_pro_banco():
    """O pre-deploy do Railway roda com a variável definida — tem de passar.

    Aqui o alembic vai até o fim da guarda e falha só na conexão (o host não
    existe), que é exatamente a prova de que a guarda deixou passar.
    """
    processo = _alembic_upgrade(**{VARIAVEL: "1"})
    saida = processo.stdout + processo.stderr

    assert processo.returncode != 0
    assert BancoNaoLocal.__name__ not in saida
    assert "failed to resolve host" in saida or "Name or service not known" in saida


@pytest.mark.parametrize("valor", ["0", "", "sim", "true", "2"])
def test_so_o_valor_1_destrava(valor):
    """Qualquer outro valor é tratado como "não definida", pra não destravar por
    engano quem exportou a variável com valor errado."""
    processo = _alembic_upgrade(**{VARIAVEL: valor})
    saida = processo.stdout + processo.stderr

    assert processo.returncode != 0
    assert BancoNaoLocal.__name__ in saida
    assert "failed to resolve host" not in saida


def test_modo_offline_nao_passa_pela_guarda():
    """`--sql` não conecta em nada, só escreve o SQL na saída: é uso legítimo pra
    revisar o que uma migração faria, mesmo com a URL de produção no env."""
    env = {k: v for k, v in os.environ.items() if k != VARIAVEL}
    env["DATABASE_URL"] = URL_REMOTA
    processo = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    saida = processo.stdout + processo.stderr

    assert processo.returncode == 0, saida
    assert BancoNaoLocal.__name__ not in saida
    assert "CREATE TABLE" in processo.stdout
