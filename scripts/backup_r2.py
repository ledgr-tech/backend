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

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

logger = logging.getLogger("backup")

PG_MAJOR = 18
TIMEOUT_HEALTHCHECK_SEGUNDOS = 10

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


def enviar(cliente, bucket: str, arquivo: Path, chaves: list[str]) -> None:
    """Envia o arquivo uma vez, na primeira chave (o diário), e faz as outras
    (o semanal, no domingo) como cópia dentro do próprio bucket."""
    original, *copias = chaves
    cliente.upload_file(str(arquivo), bucket, original)
    logger.info("enviado %s", original)
    for chave in copias:
        cliente.copy_object(
            Bucket=bucket, Key=chave, CopySource={"Bucket": bucket, "Key": original}
        )
        logger.info("copiado %s", chave)


class Healthcheck:
    """Pings no Healthchecks.io. Se o serviço estiver fora do ar, o backup segue:
    o aviso de falha é o próprio Healthchecks reclamando do ping que não veio.
    A URL é o token do check, então nunca vai para o log."""

    def __init__(self, url: str, cliente_http: httpx.Client | None = None) -> None:
        self._url = url.rstrip("/")
        self._cliente = cliente_http or httpx.Client(timeout=TIMEOUT_HEALTHCHECK_SEGUNDOS)

    def _pingar(self, sufixo: str) -> None:
        try:
            self._cliente.post(self._url + sufixo, timeout=TIMEOUT_HEALTHCHECK_SEGUNDOS)
        except httpx.HTTPError as exc:
            logger.warning("ping no Healthchecks falhou (%s)", type(exc).__name__)

    def inicio(self) -> None:
        self._pingar("/start")

    def sucesso(self) -> None:
        self._pingar("")

    def falha(self) -> None:
        self._pingar("/fail")


def executar(
    cfg: ConfigBackup,
    cliente_s3,
    healthcheck: Healthcheck,
    agora: datetime,
    *,
    versao_pg_dump: Callable[..., str],
    gerar: Callable[..., int] = gerar_dump_cifrado,
    pasta_temp: Path | None = None,
) -> int:
    """Uma execução do cron. Devolve o código de saída do processo: 0 ou 1."""
    healthcheck.inicio()
    chaves = chaves_do_dia(agora)
    logger.info("backup iniciado (%s)", chaves[0])
    descritor, caminho = tempfile.mkstemp(dir=pasta_temp, suffix=".dump.age")
    os.close(descritor)
    arquivo = Path(caminho)
    try:
        major = major_do_pg_dump(versao_pg_dump())
        if major != PG_MAJOR:
            raise BackupFalhou(f"pg_dump da major {major}; o banco é {PG_MAJOR}")
        inicio = time.monotonic()
        tamanho = gerar(cfg, arquivo)
        logger.info("dump criptografado: %d bytes em %.1f s", tamanho, time.monotonic() - inicio)
        enviar(cliente_s3, cfg.r2_bucket, arquivo, chaves)
    except Exception as exc:  # noqa: BLE001 - toda falha vira /fail e código 1
        # Só BackupFalhou tem mensagem segura; de qualquer outra exceção (boto3,
        # rede, sistema) vai só o nome da classe, porque a mensagem pode trazer
        # endpoint, bucket ou credencial.
        detalhe = str(exc) if isinstance(exc, BackupFalhou) else ""
        logger.error("backup falhou: %s %s", type(exc).__name__, detalhe)
        healthcheck.falha()
        return 1
    finally:
        arquivo.unlink(missing_ok=True)
    healthcheck.sucesso()
    logger.info("backup concluído")
    return 0


def criar_cliente_r2(cfg: ConfigBackup):
    # Import aqui dentro: o boto3 só existe na imagem do serviço de backup
    # (backup/requirements.txt), não no requirements.txt da API nem da suíte.
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        region_name="auto",
        aws_access_key_id=cfg.r2_access_key_id,
        aws_secret_access_key=cfg.r2_secret_access_key,
        # As versões novas do boto3 mandam checksums por padrão, que o R2 já
        # recusou; "when_required" volta ao comportamento antigo.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _versao_pg_dump() -> str:
    return subprocess.run(
        ["pg_dump", "--version"], capture_output=True, text=True, check=True
    ).stdout


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = ler_config(os.environ)
    except ConfigInvalida as exc:
        # Sem config não há URL de Healthchecks confiável para pingar; o check
        # avisa sozinho quando o ping do dia não chegar.
        logger.error("configuração inválida: %s", exc)
        return 1
    return executar(
        cfg,
        criar_cliente_r2(cfg),
        Healthcheck(cfg.healthcheck_url),
        datetime.now(UTC),
        versao_pg_dump=_versao_pg_dump,
    )


if __name__ == "__main__":
    sys.exit(main())
