"""Backup automático do Postgres de produção para o Cloudflare R2 (issue #103, ADR-018).

Ponto de entrada do serviço de cron do Railway (`backup/Dockerfile`), que roda
uma vez por dia às 06:00 UTC. Faz o `pg_dump` (major 18, formato custom, o
mesmo que `scripts/ensaio_restore.py` consome), criptografa com `age` para a
chave pública do time e envia ao bucket privado do R2. Avisa o Healthchecks.io
no início, no sucesso e na falha: backup que falha calado é pior que não ter
backup.

Duas regras que valem para o módulo inteiro:

- O dump sem criptografia nunca é gravado em disco. O `pg_dump` vai por pipe
  direto para o `age`, e só o arquivo criptografado passa por `/tmp`. O bucket
  (e quem tiver o token dele) só vê um arquivo ilegível.
- Regra de log da issue #34: só horário, tamanho, duração, nome do objeto e, na
  falha, o nome da classe da exceção ou o código de saída. Nunca a URL do banco,
  senha, token, chave nem a saída dos comandos (stderr do pg_dump traz host e
  usuário; a mensagem de uma exceção do boto3 traz endpoint e bucket).

Nenhum código apaga objeto do bucket: a retenção é das regras de ciclo de vida
do R2 (`diario/` com 8 dias, `semanal/` com 29). Este script nunca é rodado por
agente nem contra produção fora do serviço do Railway.
"""

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PG_MAJOR = 18

VARIAVEIS_OBRIGATORIAS = (
    "DATABASE_URL",
    "R2_ENDPOINT",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "BACKUP_AGE_RECIPIENT",
    "HEALTHCHECK_URL",
)

PREFIXO_DIARIO = "diario/"
PREFIXO_SEMANAL = "semanal/"
DOMINGO = 6  # datetime.weekday()


class ConfigInvalida(ValueError):
    """Variável obrigatória ausente ou inválida. A mensagem só traz nomes."""


class BackupFalhou(RuntimeError):
    """Etapa do backup que falhou e o código de saída. Nunca stderr: a mensagem
    vai para o log."""


@dataclass(frozen=True)
class ConfigBackup:
    # repr=False em tudo que é segredo ou carrega segredo (a URL do banco tem a
    # senha; a do Healthchecks é o próprio token do check).
    database_url: str = field(repr=False)
    r2_endpoint: str
    r2_bucket: str
    r2_access_key_id: str = field(repr=False)
    r2_secret_access_key: str = field(repr=False)
    age_recipient: str = field(repr=False)
    healthcheck_url: str = field(repr=False)


def url_para_pg_dump(url: str) -> str:
    """Tira o driver do SQLAlchemy (`postgresql+psycopg://`, o formato do serviço
    `web`), que o pg_dump não entende."""
    return re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", url)


def ler_config(env: Mapping[str, str]) -> ConfigBackup:
    valores = {nome: (env.get(nome) or "").strip() for nome in VARIAVEIS_OBRIGATORIAS}
    ausentes = [nome for nome, valor in valores.items() if not valor]
    if ausentes:
        raise ConfigInvalida(f"Variáveis obrigatórias ausentes: {', '.join(ausentes)}")

    invalidas = []
    if not valores["BACKUP_AGE_RECIPIENT"].startswith("age1"):
        invalidas.append("BACKUP_AGE_RECIPIENT (chave pública do age, começa com age1)")
    if not valores["HEALTHCHECK_URL"].startswith("https://"):
        invalidas.append("HEALTHCHECK_URL (tem de ser https)")
    if invalidas:
        raise ConfigInvalida(f"Variáveis inválidas: {', '.join(invalidas)}")

    return ConfigBackup(
        database_url=url_para_pg_dump(valores["DATABASE_URL"]),
        r2_endpoint=valores["R2_ENDPOINT"],
        r2_bucket=valores["R2_BUCKET"],
        r2_access_key_id=valores["R2_ACCESS_KEY_ID"],
        r2_secret_access_key=valores["R2_SECRET_ACCESS_KEY"],
        age_recipient=valores["BACKUP_AGE_RECIPIENT"],
        healthcheck_url=valores["HEALTHCHECK_URL"],
    )


def major_do_pg_dump(saida_version: str) -> int:
    """Major a partir da saída de `pg_dump --version`."""
    achado = re.search(r"\(PostgreSQL\)\s+(\d+)", saida_version)
    if not achado:
        raise ValueError("Saída de `pg_dump --version` ilegível")
    return int(achado.group(1))


def chaves_do_dia(agora: datetime) -> list[str]:
    """Objetos de uma execução: o diário sempre, e a cópia semanal no domingo
    (UTC). O nome traz o horário em UTC, que é o que dá o RPO."""
    utc = agora.astimezone(UTC)
    nome = f"ledgr-{utc:%Y%m%d-%H%M}.dump.age"
    chaves = [PREFIXO_DIARIO + nome]
    if utc.weekday() == DOMINGO:
        chaves.append(PREFIXO_SEMANAL + nome)
    return chaves


def comando_pg_dump(url: str) -> list[str]:
    return ["pg_dump", "--format=custom", "--no-owner", f"--dbname={url}"]


def comando_age(recipient: str, destino: Path) -> list[str]:
    return ["age", "--recipient", recipient, "--output", str(destino)]


def gerar_dump_cifrado(
    cfg: ConfigBackup,
    destino: Path,
    *,
    comando_dump: list[str] | None = None,
    comando_cifra: list[str] | None = None,
) -> int:
    """`pg_dump | age` direto para `destino`, sem o dump legível passar pelo
    disco. Devolve o tamanho do arquivo criptografado. Em qualquer falha apaga
    `destino`, para nunca sobrar (nem ser enviado) um arquivo parcial.

    `comando_dump` e `comando_cifra` existem só para os testes trocarem os
    binários por dublês."""
    comando_dump = comando_dump or comando_pg_dump(cfg.database_url)
    comando_cifra = comando_cifra or comando_age(cfg.age_recipient, destino)
    try:
        # stderr=DEVNULL nos dois: o do pg_dump traz host e usuário, e o que
        # sai no stderr do container vira log no Railway (regra da #34).
        dump = subprocess.Popen(comando_dump, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cifra = subprocess.Popen(
            comando_cifra, stdin=dump.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Sem esta cópia no pai, o pg_dump não recebe SIGPIPE se o age morrer
        # e fica bloqueado escrevendo num pipe que ninguém lê.
        dump.stdout.close()
        codigo_cifra = cifra.wait()
        codigo_dump = dump.wait()
        if codigo_dump != 0:
            raise BackupFalhou(f"pg_dump saiu com código {codigo_dump}")
        if codigo_cifra != 0:
            raise BackupFalhou(f"age saiu com código {codigo_cifra}")
        tamanho = destino.stat().st_size if destino.exists() else 0
        if tamanho == 0:
            raise BackupFalhou("dump criptografado vazio")
        return tamanho
    except BaseException:
        destino.unlink(missing_ok=True)
        raise
