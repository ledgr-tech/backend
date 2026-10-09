import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.config import settings
from app.models import Base  # importa todos os models registrados

# Destrava migração contra banco remoto. Definida no serviço do backend no
# Railway, onde o pre-deploy (`alembic upgrade head`) precisa migrar produção de
# propósito (issue #65). Nunca entra no .env local.
VARIAVEL_BANCO_REMOTO = "LEDGR_MIGRAR_BANCO_REMOTO"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# a connection string vem de DATABASE_URL (via app.core.config), nunca do
# alembic.ini commitado no repo
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def exigir_destino_permitido(url: str) -> None:
    """Recusa migrar banco remoto sem `LEDGR_MIGRAR_BANCO_REMOTO=1` (issue #65).

    A guarda é contra ACIDENTE, não contra quem decide migrar produção. O
    acidente concreto que ela cobre: o `.env` do repositório aponta DATABASE_URL
    pro Railway, então um `alembic upgrade head` rodado na máquina de
    desenvolvimento, sem DATABASE_URL explícito no comando, migra produção sem
    avisar — e desde esta issue o deploy migra sozinho, então ninguém mais tem
    motivo pra fazer isso à mão.

    Quem define a variável está dizendo "sei que é remoto e é isso que eu
    quero": é o pre-deploy do Railway, que roda num container com as variáveis do
    serviço. A guarda não tenta impedir essa pessoa, e não deve — não é controle
    de acesso, é cinto de segurança. O que protege o banco de verdade é a
    credencial, que não está aqui.

    Só no modo online: `--sql` (offline) não conecta em nada, só escreve SQL na
    saída, e esse uso é legítimo pra revisar o que uma migração faria.
    """
    if os.getenv(VARIAVEL_BANCO_REMOTO) == "1":
        return
    try:
        exigir_banco_local(url)
    except BancoNaoLocal as erro:
        raise BancoNaoLocal(
            f"{erro}\n\n"
            "Alembic recusou migrar banco que não é local. Em produção isso é "
            f"trabalho do pre-deploy do Railway, que roda com {VARIAVEL_BANCO_REMOTO}=1 "
            "nas variáveis do serviço — não rode à mão daqui.\n\n"
            "Pra migrar o Postgres local, passe DATABASE_URL explícito no comando:\n"
            "  DATABASE_URL=postgresql+psycopg://ledgr:ledgr@127.0.0.1:55432/ledgr \\\n"
            "    alembic upgrade head\n\n"
            "O .env do repositório aponta pro Railway, e é justamente esse acidente "
            "que esta guarda existe pra evitar."
        ) from erro


# Limites da migração online (issue #102). O pre-deploy do Railway passa a ter
# 120 s; em 08/10 um pre-deploy ficou parado 300 s depois de conectar, até o
# Railway matar, sem dizer o que esperava. Com estes limites o alembic falha
# sozinho, com a causa no log, antes de o Railway desistir:
# - 10 s para conectar: um banco que não responde nesse tempo não vai responder;
# - 10 s esperando lock: a migração disputa tabela com a API, e uma consulta
#   presa segurando o lock não pode travar o deploy inteiro;
# - 90 s por comando: maior que qualquer migração de hoje e menor que os 120 s,
#   para sobrar tempo de o erro chegar ao log.
CONNECT_TIMEOUT_SEGUNDOS = 10
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "90s"


def run_migrations_online() -> None:
    exigir_destino_permitido(config.get_main_option("sqlalchemy.url"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SEGUNDOS},
    )
    with connectable.connect() as connection:
        # SET de sessão, não SET LOCAL: vale para a conexão inteira, inclusive
        # a transação da migração aberta abaixo. O commit fecha a transação que
        # o SQLAlchemy abriu sozinho para os SETs; sem ele o begin_transaction
        # do alembic veria uma transação em curso e não faria commit da migração.
        connection.exec_driver_sql(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        connection.exec_driver_sql(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
