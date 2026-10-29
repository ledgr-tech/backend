"""scripts/backup_r2.py (issue #103, ADR-018): configuração, nome dos objetos
no bucket e conferência da major do pg_dump.

Nada aqui toca banco, R2 ou rede: só funções puras sobre variáveis de ambiente,
datas e a saída de `pg_dump --version`.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from scripts import backup_r2 as b

ENV_OK = {
    "DATABASE_URL": "postgresql://u:senha-secreta@postgres.railway.internal:5432/railway",
    "R2_ENDPOINT": "https://abc.r2.cloudflarestorage.com",
    "R2_BUCKET": "ledgr-backups",
    "R2_ACCESS_KEY_ID": "id",
    "R2_SECRET_ACCESS_KEY": "segredo-r2",
    "BACKUP_AGE_RECIPIENT": "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq",
    "HEALTHCHECK_URL": "https://hc-ping.com/uuid-do-check",
}


def test_ler_config_completa():
    cfg = b.ler_config(ENV_OK)
    assert cfg.r2_bucket == "ledgr-backups"


@pytest.mark.parametrize("faltando", b.VARIAVEIS_OBRIGATORIAS)
def test_ler_config_recusa_variavel_vazia(faltando):
    env = {**ENV_OK, faltando: "  "}
    with pytest.raises(b.ConfigInvalida) as erro:
        b.ler_config(env)
    assert faltando in str(erro.value)
    assert "senha-secreta" not in str(erro.value)


@pytest.mark.parametrize("faltando", b.VARIAVEIS_OBRIGATORIAS)
def test_ler_config_recusa_variavel_ausente(faltando):
    env = {k: v for k, v in ENV_OK.items() if k != faltando}
    with pytest.raises(b.ConfigInvalida, match=faltando):
        b.ler_config(env)


def test_variaveis_obrigatorias_na_ordem_do_adr():
    assert b.VARIAVEIS_OBRIGATORIAS == (
        "DATABASE_URL",
        "R2_ENDPOINT",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "BACKUP_AGE_RECIPIENT",
        "HEALTHCHECK_URL",
    )


def test_recipient_tem_de_ser_age1():
    with pytest.raises(b.ConfigInvalida, match="BACKUP_AGE_RECIPIENT"):
        b.ler_config({**ENV_OK, "BACKUP_AGE_RECIPIENT": "ssh-ed25519 AAAA"})


def test_healthcheck_tem_de_ser_https():
    with pytest.raises(b.ConfigInvalida, match="HEALTHCHECK_URL"):
        b.ler_config({**ENV_OK, "HEALTHCHECK_URL": "http://hc-ping.com/x"})


def test_repr_nao_mostra_segredos():
    texto = repr(b.ler_config(ENV_OK))
    assert "senha-secreta" not in texto
    assert "segredo-r2" not in texto
    assert "uuid-do-check" not in texto


def test_url_psycopg_vira_postgresql():
    url = "postgresql+psycopg://u:p@h:5432/db"
    assert b.url_para_pg_dump(url) == "postgresql://u:p@h:5432/db"
    assert b.url_para_pg_dump("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_ler_config_normaliza_url():
    cfg = b.ler_config({**ENV_OK, "DATABASE_URL": "postgresql+psycopg://u:p@h/db"})
    assert cfg.database_url == "postgresql://u:p@h/db"


@pytest.mark.parametrize(
    "saida,major",
    [
        ("pg_dump (PostgreSQL) 18.1 (Debian 18.1-1.pgdg13+1)\n", 18),
        ("pg_dump (PostgreSQL) 16.4\n", 16),
    ],
)
def test_major_do_pg_dump(saida, major):
    assert b.major_do_pg_dump(saida) == major


def test_major_do_pg_dump_ilegivel():
    with pytest.raises(ValueError):
        b.major_do_pg_dump("comando não encontrado")


def test_chaves_dia_comum():
    agora = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # terça
    assert b.chaves_do_dia(agora) == ["diario/ledgr-20261020-0600.dump.age"]


def test_chaves_domingo_inclui_semanal():
    agora = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)  # domingo
    assert b.chaves_do_dia(agora) == [
        "diario/ledgr-20261018-0600.dump.age",
        "semanal/ledgr-20261018-0600.dump.age",
    ]


def test_chaves_usa_utc():
    # 23:30 de sábado em Brasília = 02:30 de domingo em UTC
    agora = datetime(2026, 10, 17, 23, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert b.chaves_do_dia(agora)[1] == "semanal/ledgr-20261018-0230.dump.age"


def test_r2_endpoint_tem_de_ser_https():
    with pytest.raises(b.ConfigInvalida, match="R2_ENDPOINT"):
        b.ler_config({**ENV_OK, "R2_ENDPOINT": "abc.r2.cloudflarestorage.com"})
