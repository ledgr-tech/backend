"""Baixa um backup do R2 e decifra com a chave privada do age (issue #103, ADR-018).

Primeira metade do ensaio de restore a partir do backup automático. Rodado por
uma pessoa, na própria máquina, nunca por agente. A segunda metade é o
`scripts/ensaio_restore.py`, que restaura o `.dump` num Postgres descartável:
este script imprime o comando no fim.

Usa um token do R2 **só de leitura**, diferente do token do serviço de backup,
nas variáveis R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID e R2_SECRET_ACCESS_KEY.
A chave privada do age (guardada no gerenciador de senhas do Vitor e do Rafael)
vai num arquivo temporário passado em --identidade; apague-o no fim.

O `.dump` decifrado é uma cópia integral do banco de produção: fica fora do
repositório (o script recusa um destino dentro dele) e deve ser apagado depois
do ensaio, como no roteiro manual de 07-tecnico/backup-e-restore.md.

Uso:

    ./venv/bin/python scripts/baixar_backup_r2.py --identidade /caminho/chave-age.txt
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/baixar_backup_r2.py`, o sys.path recebe scripts/,
# não a raiz do repositório (mesmo padrão do ensaio_restore.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts.backup_r2 import PREFIXO_DIARIO, cliente_r2

VARIAVEIS_R2 = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def comando_decifra_age(identidade: Path, entrada: Path, saida: Path) -> list[str]:
    return [
        "age",
        "--decrypt",
        "--identity",
        str(identidade),
        "--output",
        str(saida),
        str(entrada),
    ]


def mais_recente(chaves: list[str], prefixo: str = PREFIXO_DIARIO) -> str:
    """O nome traz a data e a hora em UTC, então o maior nome é o mais novo."""
    do_prefixo = [chave for chave in chaves if chave.startswith(prefixo)]
    if not do_prefixo:
        raise ValueError(f"Nenhum backup em {prefixo}")
    return max(do_prefixo)


def exigir_fora_do_repositorio(destino: Path) -> None:
    destino = destino.expanduser().resolve()
    if destino == RAIZ or RAIZ in destino.parents:
        raise ValueError(
            f"{destino} fica dentro do repositório. O dump é uma cópia do banco de "
            f"produção e não pode ficar aqui; use uma pasta como ~/backups/ledgr."
        )


def baixar_e_decifrar(
    cliente,
    bucket: str,
    chave: str,
    identidade: Path,
    destino: Path,
    *,
    montar_decifra: Callable[..., list[str]] = comando_decifra_age,
) -> Path:
    """Baixa `chave` para `destino`, decifra e apaga o `.age`. Devolve o `.dump`.
    Se a decifra falhar, não sobra nenhum dos dois arquivos."""
    nome = Path(chave).name
    cifrado = destino / nome
    dump = destino / nome.removesuffix(".age")
    try:
        cliente.download_file(bucket, chave, str(cifrado))
        processo = subprocess.run(
            montar_decifra(identidade, cifrado, dump), capture_output=True, text=True, check=False
        )
        if processo.returncode != 0:
            raise RuntimeError(f"age saiu com código {processo.returncode}")
    except BaseException:
        dump.unlink(missing_ok=True)
        raise
    finally:
        cifrado.unlink(missing_ok=True)
    return dump


def _argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--identidade", type=Path, required=True, help="arquivo da chave privada do age"
    )
    parser.add_argument(
        "--destino",
        type=Path,
        default=Path("~/backups/ledgr"),
        help="pasta fora do repositório (padrão: ~/backups/ledgr)",
    )
    parser.add_argument(
        "--objeto", help="chave do objeto no bucket (padrão: o diário mais recente)"
    )
    return parser.parse_args()


def main() -> int:
    args = _argumentos()
    ausentes = [nome for nome in VARIAVEIS_R2 if not os.environ.get(nome, "").strip()]
    if ausentes:
        print(f"Variáveis ausentes: {', '.join(ausentes)}", file=sys.stderr)
        return 1
    destino = args.destino.expanduser()
    try:
        exigir_fora_do_repositorio(destino)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    destino.mkdir(parents=True, exist_ok=True)

    bucket = os.environ["R2_BUCKET"]
    cliente = cliente_r2(
        os.environ["R2_ENDPOINT"],
        os.environ["R2_ACCESS_KEY_ID"],
        os.environ["R2_SECRET_ACCESS_KEY"],
    )
    chave = args.objeto
    if not chave:
        resposta = cliente.list_objects_v2(Bucket=bucket, Prefix=PREFIXO_DIARIO)
        chave = mais_recente([item["Key"] for item in resposta.get("Contents", [])])
    print(f"Baixando {chave}")
    dump = baixar_e_decifrar(cliente, bucket, chave, args.identidade, destino)
    print(f"Decifrado em {dump}")
    print("Próximo passo, no diretório do backend:")
    print(f"  ./venv/bin/python scripts/ensaio_restore.py --dump {dump}")
    print("Depois do ensaio, apague o .dump e o arquivo da chave privada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
