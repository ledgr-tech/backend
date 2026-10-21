"""scripts/baixar_backup_r2.py (issue #103, ADR-018): baixa um backup do R2 e
decifra com a chave privada do age, para o ensaio de restore.

O R2 é um dublê e o age é trocado por comandos Python reais (copia ou falha),
como em test_backup_r2_dump.py.
"""

import sys
from pathlib import Path

import pytest

from scripts import baixar_backup_r2 as s

PY = sys.executable
CHAVE = "diario/ledgr-20261020-0600.dump.age"


class ClienteFalso:
    def __init__(self, conteudo: bytes) -> None:
        self.conteudo = conteudo
        self.baixados: list[tuple[str, str]] = []

    def download_file(self, bucket, chave, caminho):
        self.baixados.append((bucket, chave))
        Path(caminho).write_bytes(self.conteudo)


def copia(identidade: Path, entrada: Path, saida: Path) -> list[str]:
    codigo = f"import shutil; shutil.copyfile({str(entrada)!r}, {str(saida)!r})"
    return [PY, "-c", codigo]


def falha(identidade: Path, entrada: Path, saida: Path) -> list[str]:
    codigo = f"open({str(saida)!r}, 'wb').write(b'parcial'); raise SystemExit(1)"
    return [PY, "-c", codigo]


def test_comando_decifra_age():
    assert s.comando_decifra_age(Path("/k"), Path("/e.age"), Path("/s.dump")) == [
        "age",
        "--decrypt",
        "--identity",
        "/k",
        "--output",
        "/s.dump",
        "/e.age",
    ]


def test_mais_recente():
    chaves = [
        "diario/ledgr-20261018-0600.dump.age",
        "diario/ledgr-20261020-0600.dump.age",
        "semanal/ledgr-20261025-0600.dump.age",
    ]
    assert s.mais_recente(chaves) == "diario/ledgr-20261020-0600.dump.age"
    assert s.mais_recente(chaves, "semanal/") == "semanal/ledgr-20261025-0600.dump.age"


def test_mais_recente_vazio():
    with pytest.raises(ValueError):
        s.mais_recente(["semanal/a"], "diario/")
    with pytest.raises(ValueError):
        s.mais_recente([])


def test_recusa_destino_no_repositorio():
    with pytest.raises(ValueError):
        s.exigir_fora_do_repositorio(s.RAIZ / "tmp")
    with pytest.raises(ValueError):
        s.exigir_fora_do_repositorio(s.RAIZ)
    s.exigir_fora_do_repositorio(Path("/tmp/fora"))  # não levanta


def test_baixa_e_decifra(tmp_path):
    cliente = ClienteFalso(b"PGDMP")
    caminho = s.baixar_e_decifrar(
        cliente, "b", CHAVE, tmp_path / "id.txt", tmp_path, montar_decifra=copia
    )
    assert cliente.baixados == [("b", CHAVE)]
    assert caminho == tmp_path / "ledgr-20261020-0600.dump"
    assert caminho.read_bytes() == b"PGDMP"
    assert not (tmp_path / "ledgr-20261020-0600.dump.age").exists()


def test_decifra_falha_apaga_tudo(tmp_path):
    with pytest.raises(RuntimeError):
        s.baixar_e_decifrar(
            ClienteFalso(b"x"), "b", CHAVE, tmp_path / "id.txt", tmp_path, montar_decifra=falha
        )
    assert list(tmp_path.iterdir()) == []
