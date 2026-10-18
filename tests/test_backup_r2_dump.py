"""scripts/backup_r2.py (issue #103, ADR-018): pg_dump por pipe para o age.

Os dublês de `pg_dump` e `age` são processos Python de verdade, então o pipe
entre os dois é exercitado de fato: só o comando muda, não o encanamento.
"""

import sys
from pathlib import Path

import pytest

from scripts import backup_r2 as b
from tests.test_backup_r2_config import ENV_OK

CFG = b.ler_config(ENV_OK)
PY = sys.executable
GERA = [PY, "-c", "import sys; sys.stdout.buffer.write(b'PGDMP' * 1000)"]
FALHA = [PY, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(3)"]


def copia(destino: Path) -> list[str]:
    """Faz o papel do age: copia o stdin para `destino`."""
    codigo = (
        "import sys, shutil; " f"shutil.copyfileobj(sys.stdin.buffer, open({str(destino)!r}, 'wb'))"
    )
    return [PY, "-c", codigo]


def test_comandos_reais():
    assert b.comando_pg_dump("postgresql://h/db") == [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--dbname=postgresql://h/db",
    ]
    assert b.comando_age("age1x", Path("/tmp/a")) == [
        "age",
        "--recipient",
        "age1x",
        "--output",
        "/tmp/a",
    ]


def test_pipe_grava_e_devolve_tamanho(tmp_path):
    destino = tmp_path / "x.dump.age"
    tamanho = b.gerar_dump_cifrado(CFG, destino, comando_dump=GERA, comando_cifra=copia(destino))
    assert tamanho == 5000
    assert destino.read_bytes().startswith(b"PGDMP")


def test_pg_dump_falha(tmp_path):
    destino = tmp_path / "x.dump.age"
    sai_2 = [PY, "-c", "import sys; sys.exit(2)"]
    with pytest.raises(b.BackupFalhou, match="pg_dump.*2"):
        b.gerar_dump_cifrado(CFG, destino, comando_dump=sai_2, comando_cifra=copia(destino))
    assert not destino.exists()


def test_age_falha_apaga_parcial(tmp_path):
    destino = tmp_path / "x.dump.age"
    destino.write_bytes(b"parcial")
    with pytest.raises(b.BackupFalhou, match="age.*3"):
        b.gerar_dump_cifrado(CFG, destino, comando_dump=GERA, comando_cifra=FALHA)
    assert not destino.exists()


def test_dump_vazio_e_falha(tmp_path):
    destino = tmp_path / "x.dump.age"
    vazio = [PY, "-c", "pass"]
    with pytest.raises(b.BackupFalhou, match="vazio"):
        b.gerar_dump_cifrado(CFG, destino, comando_dump=vazio, comando_cifra=copia(destino))
    assert not destino.exists()


def test_mensagem_de_falha_nao_traz_stderr(tmp_path, capfd):
    destino = tmp_path / "x.dump.age"
    vaza = [PY, "-c", "import sys; sys.stderr.write('senha-secreta'); sys.exit(1)"]
    with pytest.raises(b.BackupFalhou) as erro:
        b.gerar_dump_cifrado(CFG, destino, comando_dump=vaza, comando_cifra=copia(destino))
    assert "senha-secreta" not in str(erro.value)
    # nem no stderr do processo (que vira log no Railway)
    assert "senha-secreta" not in capfd.readouterr().err


def test_age_que_morre_cedo_e_culpado_nao_o_pg_dump(tmp_path):
    # O age recusa a chave e sai sem ler nada; o pg_dump, escrevendo num pipe
    # sem leitor, morre por SIGPIPE. A causa a apontar é o age.
    destino = tmp_path / "x.dump.age"
    muito = [PY, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"]
    sai_ja = [PY, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(b.BackupFalhou, match="age.*3"):
        b.gerar_dump_cifrado(CFG, destino, comando_dump=muito, comando_cifra=sai_ja)
    assert not destino.exists()


def test_dump_travado_estoura_o_tempo_e_nao_deixa_arquivo(tmp_path):
    # pg_dump esperando lock para sempre: o cron do Railway pula as próximas
    # execuções enquanto esta não terminar, então o processo tem de sair.
    destino = tmp_path / "x.dump.age"
    travado = [PY, "-c", "import time\nwhile True: time.sleep(1)"]
    with pytest.raises(b.BackupFalhou, match="tempo"):
        b.gerar_dump_cifrado(
            CFG, destino, comando_dump=travado, comando_cifra=copia(destino), timeout_segundos=1
        )
    assert not destino.exists()
