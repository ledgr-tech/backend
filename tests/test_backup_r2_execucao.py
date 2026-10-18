"""scripts/backup_r2.py (issue #103, ADR-018): envio ao R2, aviso no
Healthchecks.io e a execução de ponta a ponta.

R2 e Healthchecks são dublês (o cliente S3 registra as chamadas; o Healthchecks
real roda sobre um transporte falso do httpx). O dump é trocado por uma função
que grava bytes fixos: o pipe de verdade é coberto em test_backup_r2_dump.py.
"""

import logging
from datetime import UTC, datetime

import httpx

from scripts import backup_r2 as b
from tests.test_backup_r2_config import ENV_OK

CFG = b.ler_config(ENV_OK)
DOMINGO = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
TERCA = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
SEGREDOS = ("senha-secreta", "segredo-r2", "uuid-do-check", "age1qyqs", "cloudflarestorage")


def versao_ok() -> str:
    return "pg_dump (PostgreSQL) 18.1"


def gera_ok(cfg, destino):
    destino.write_bytes(b"x" * 10)
    return 10


class ClienteS3Falso:
    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.uploads: list[tuple[str, str]] = []
        self.copias: list[tuple[str, str]] = []

    def upload_file(self, arquivo, bucket, chave):
        if self.erro:
            raise self.erro
        self.uploads.append((bucket, chave))

    def copy_object(self, Bucket, Key, CopySource):
        assert CopySource["Bucket"] == Bucket
        self.copias.append((Key, CopySource["Key"]))


class HealthcheckFalso:
    def __init__(self) -> None:
        self.chamadas: list[str] = []

    def inicio(self):
        self.chamadas.append("inicio")

    def sucesso(self):
        self.chamadas.append("sucesso")

    def falha(self):
        self.chamadas.append("falha")


def _healthcheck_fora_do_ar() -> b.Healthcheck:
    def recusa(requisicao):
        raise httpx.ConnectError("sem rede", request=requisicao)

    cliente = httpx.Client(transport=httpx.MockTransport(recusa))
    return b.Healthcheck("https://hc-ping.com/uuid-do-check", cliente)


def test_sucesso_envia_diario_e_copia_semanal(tmp_path):
    s3, hc = ClienteS3Falso(), HealthcheckFalso()
    rc = b.executar(
        CFG, s3, hc, DOMINGO, versao_pg_dump=versao_ok, gerar=gera_ok, pasta_temp=tmp_path
    )
    assert rc == 0
    assert s3.uploads == [("ledgr-backups", "diario/ledgr-20261018-0600.dump.age")]
    assert s3.copias == [
        ("semanal/ledgr-20261018-0600.dump.age", "diario/ledgr-20261018-0600.dump.age")
    ]
    assert hc.chamadas == ["inicio", "sucesso"]
    assert list(tmp_path.iterdir()) == []


def test_dia_comum_nao_copia_semanal(tmp_path):
    s3 = ClienteS3Falso()
    b.executar(
        CFG,
        s3,
        HealthcheckFalso(),
        TERCA,
        versao_pg_dump=versao_ok,
        gerar=gera_ok,
        pasta_temp=tmp_path,
    )
    assert s3.uploads == [("ledgr-backups", "diario/ledgr-20261020-0600.dump.age")]
    assert s3.copias == []


def test_major_errada_nao_gera_nem_envia(tmp_path):
    s3, hc = ClienteS3Falso(), HealthcheckFalso()
    gerou = []

    def gera(cfg, destino):
        gerou.append(destino)
        return 1

    rc = b.executar(
        CFG,
        s3,
        hc,
        DOMINGO,
        versao_pg_dump=lambda: "pg_dump (PostgreSQL) 16.4",
        gerar=gera,
        pasta_temp=tmp_path,
    )
    assert rc == 1
    assert gerou == []
    assert s3.uploads == []
    assert hc.chamadas == ["inicio", "falha"]


def test_falha_no_dump_nao_envia(tmp_path):
    s3, hc = ClienteS3Falso(), HealthcheckFalso()

    def gera(cfg, destino):
        raise b.BackupFalhou("age saiu com código 1")

    rc = b.executar(CFG, s3, hc, DOMINGO, versao_pg_dump=versao_ok, gerar=gera, pasta_temp=tmp_path)
    assert rc == 1
    assert s3.uploads == []
    assert hc.chamadas == ["inicio", "falha"]
    assert list(tmp_path.iterdir()) == []


def test_falha_no_r2_nao_vaza_mensagem(tmp_path, caplog):
    s3 = ClienteS3Falso(erro=RuntimeError("https://abc.r2.cloudflarestorage.com segredo-r2"))
    hc = HealthcheckFalso()
    with caplog.at_level(logging.INFO, logger="backup"):
        rc = b.executar(
            CFG, s3, hc, DOMINGO, versao_pg_dump=versao_ok, gerar=gera_ok, pasta_temp=tmp_path
        )
    assert rc == 1
    assert hc.chamadas == ["inicio", "falha"]
    assert "RuntimeError" in caplog.text
    for segredo in SEGREDOS:
        assert segredo not in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_log_de_sucesso_so_tem_metadado(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="backup"):
        b.executar(
            CFG,
            ClienteS3Falso(),
            HealthcheckFalso(),
            DOMINGO,
            versao_pg_dump=versao_ok,
            gerar=gera_ok,
            pasta_temp=tmp_path,
        )
    assert "diario/ledgr-20261018-0600.dump.age" in caplog.text
    assert "10 bytes" in caplog.text
    for segredo in SEGREDOS:
        assert segredo not in caplog.text


def test_healthcheck_fora_do_ar_nao_derruba(caplog):
    hc = _healthcheck_fora_do_ar()
    with caplog.at_level(logging.WARNING, logger="backup"):
        hc.inicio()
        hc.sucesso()
        hc.falha()
    assert "ConnectError" in caplog.text
    assert "uuid-do-check" not in caplog.text


def test_backup_sai_mesmo_com_healthcheck_fora_do_ar(tmp_path):
    s3 = ClienteS3Falso()
    rc = b.executar(
        CFG,
        s3,
        _healthcheck_fora_do_ar(),
        TERCA,
        versao_pg_dump=versao_ok,
        gerar=gera_ok,
        pasta_temp=tmp_path,
    )
    assert rc == 0
    assert s3.uploads == [("ledgr-backups", "diario/ledgr-20261020-0600.dump.age")]


def test_healthcheck_urls():
    vistas = []

    def responde(requisicao):
        vistas.append((requisicao.method, str(requisicao.url)))
        return httpx.Response(200)

    hc = b.Healthcheck(
        "https://hc-ping.com/u", httpx.Client(transport=httpx.MockTransport(responde))
    )
    hc.inicio()
    hc.sucesso()
    hc.falha()
    assert vistas == [
        ("POST", "https://hc-ping.com/u/start"),
        ("POST", "https://hc-ping.com/u"),
        ("POST", "https://hc-ping.com/u/fail"),
    ]


def test_main_sem_config_devolve_1_sem_pingar(monkeypatch, caplog):
    for nome in b.VARIAVEIS_OBRIGATORIAS:
        monkeypatch.delenv(nome, raising=False)

    def proibido(*args, **kwargs):
        raise AssertionError("não pode pingar sem config")

    monkeypatch.setattr(b.Healthcheck, "__init__", proibido)
    with caplog.at_level(logging.ERROR, logger="backup"):
        assert b.main() == 1
    assert "DATABASE_URL" in caplog.text
