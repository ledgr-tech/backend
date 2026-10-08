"""Ensaio de restore do backup do Postgres de produção (issue #35).

Sobe um Postgres descartável em Docker, restaura nele um dump no formato custom
gerado por uma pessoa (nunca por este script — o roteiro do dump está em
`07-tecnico/backup-e-restore.md`, no vault) e, em cima da cópia restaurada,
roda `alembic upgrade head` e `alembic check`. Com isso o ensaio cobre as duas
perguntas de uma vez: o backup volta? e a migração pendente roda em cima de
dado de produção de verdade? (esta segunda é o ensaio pedido pela issue #65).

Nada aqui toca o banco do Railway. A URL de conexão é montada pelo próprio
script apontando pra 127.0.0.1, e `exigir_banco_local` (de
`app/core/banco_local.py`, compartilhada com o conftest e o `migrations/env.py`
desde a issue #65) barra qualquer host que não seja local antes de cada comando
que recebe DATABASE_URL. A guarda não é decorativa: `app/core/config.py` lê o
`.env` do repositório, que em máquina de desenvolvimento aponta pro Railway,
então um subprocesso de alembic sem DATABASE_URL no env migraria produção. O que
garante o destino certo é a precedência da variável de ambiente sobre o `.env` no
pydantic-settings (conferido na 2.15.0, a versão de requirements.txt).

Uso:

    ./venv/bin/python scripts/ensaio_restore.py --dump ~/backups/ledgr-20261008-1030.dump

Sai com código 0 só quando todas as conferências passam. Se a migração falhar,
o container fica de pé pra inspeção (o comando de remoção sai no relatório e no
terminal).
"""

import argparse
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/ensaio_restore.py`, o sys.path recebe scripts/, não
# a raiz do repositório, e o import de app/ abaixo não acharia nada. Pytest não
# passa por aqui (pythonpath = ["."] no pyproject.toml), mas a linha de comando
# é o uso normal deste script.
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.core.banco_local import exigir_banco_local

USUARIO_PG = "postgres"
BANCO_ENSAIO = "ensaio"
CONSTRAINT_TOLERANCIA = "ck_configuracoes_tolerancia_dias_nao_negativa"
# Espelho de TOLERANCIA_DIAS_MAXIMA em app/api/empresa.py. O banco só garante o
# mínimo (CHECK >= 0); o máximo é regra de produto da API (issue #85), então
# nenhuma linha restaurada deveria passar de 5 — é isso que o ensaio confere.
TOLERANCIA_DIAS_MAXIMA = 5
SEGUNDOS_ESPERA_BANCO = 90
CAMINHO_DUMP_NO_CONTAINER = "/tmp/ensaio.dump"


class EnsaioFalhou(RuntimeError):
    """Falha depois do container de pé: ele é mantido pra inspeção."""


def _rodar(comando: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    # check=False de propósito: todo chamador decide o que fazer com o
    # returncode, e vários (pg_isready na espera, alembic) esperam falha.
    return subprocess.run(comando, capture_output=True, text=True, check=False, **kwargs)


def _docker(*args: str) -> str:
    processo = _rodar(["docker", *args])
    if processo.returncode != 0:
        raise RuntimeError(f"`docker {' '.join(args)}` falhou:\n{processo.stderr.strip()}")
    return processo.stdout


def _porta_aberta(porta: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", porta)) == 0


def subir_container(pg_major: int, porta: int, senha: str) -> str:
    """Sobe o Postgres descartável e devolve o nome do container."""
    if _porta_aberta(porta):
        raise RuntimeError(
            f"A porta {porta} já está ocupada em 127.0.0.1. Escolha outra em --porta "
            f"ou derrube o que está lá."
        )
    nome = f"ledgr-ensaio-restore-{porta}-{secrets.token_hex(3)}"
    _docker(
        "run",
        "--detach",
        "--name",
        nome,
        # Publicado só no loopback: o banco do ensaio nunca aparece na rede.
        "--publish",
        f"127.0.0.1:{porta}:5432",
        # Sem `--volume` de propósito, e não é só por ser descartável: na imagem
        # 18 o PGDATA saiu de /var/lib/postgresql/data pra
        # /var/lib/postgresql/18/docker, então um mount no caminho antigo seria
        # aceito em silêncio e não guardaria nada.
        "--env",
        f"POSTGRES_PASSWORD={senha}",
        "--env",
        f"POSTGRES_USER={USUARIO_PG}",
        "--env",
        f"POSTGRES_DB={BANCO_ENSAIO}",
        f"postgres:{pg_major}",
    )
    return nome


def esperar_banco(container: str, porta: int) -> None:
    limite = time.monotonic() + SEGUNDOS_ESPERA_BANCO
    while time.monotonic() < limite:
        # `-h 127.0.0.1` de propósito: durante o initdb a imagem sobe um
        # servidor temporário que escuta só no socket unix, e um pg_isready sem
        # host diria "pronto" antes da hora. A porta publicada é conferida
        # junto porque é por ela que o alembic entra, do lado de fora.
        pronto = _rodar(
            ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1"]
            + ["-U", USUARIO_PG, "-d", BANCO_ENSAIO]
        )
        if pronto.returncode == 0 and _porta_aberta(porta):
            return
        time.sleep(1)
    logs = _rodar(["docker", "logs", "--tail", "30", container]).stderr
    raise RuntimeError(
        f"O Postgres do container não ficou pronto em {SEGUNDOS_ESPERA_BANCO}s.\n{logs}"
    )


def restaurar(container: str, dump: Path) -> float:
    """Copia o dump pra dentro do container, restaura e devolve o tempo gasto (RTO)."""
    _docker("cp", str(dump), f"{container}:{CAMINHO_DUMP_NO_CONTAINER}")
    comando = [
        "docker",
        "exec",
        container,
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "-U",
        USUARIO_PG,
        "-d",
        BANCO_ENSAIO,
        CAMINHO_DUMP_NO_CONTAINER,
    ]
    inicio = time.monotonic()
    processo = _rodar(comando)
    duracao = time.monotonic() - inicio
    if processo.returncode != 0:
        raise EnsaioFalhou(f"pg_restore falhou:\n{processo.stderr.strip()}")
    return duracao


def _psql(container: str, sql: str) -> list[list[str]]:
    """Roda SQL dentro do container e devolve as linhas já quebradas por campo."""
    saida = _docker(
        "exec",
        container,
        "psql",
        "-U",
        USUARIO_PG,
        "-d",
        BANCO_ENSAIO,
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-F",
        "\t",
        "-c",
        sql,
    )
    return [linha.split("\t") for linha in saida.splitlines() if linha]


def _escalar(container: str, sql: str) -> str:
    linhas = _psql(container, sql)
    return linhas[0][0] if linhas else ""


def _tabela_existe(container: str, nome: str) -> bool:
    return _escalar(container, f"SELECT to_regclass('public.{nome}') IS NOT NULL") == "t"


@dataclass
class Inventario:
    """Retrato do banco restaurado: só contagens, nomes e metadado de schema.

    De propósito não guarda nenhuma linha de nenhuma tabela de negócio — é o que
    deixa o relatório publicável num PR sem vazar dado de cliente.
    """

    versoes_alembic: list[str]
    contagens: dict[str, int]
    distribuicao_tolerancia: list[tuple[str, int]]
    column_default: str
    acima_do_maximo: int | None
    constraint_presente: bool


def coletar_inventario(container: str) -> Inventario:
    versoes: list[str] = []
    if _tabela_existe(container, "alembic_version"):
        versoes = [
            linha[0]
            for linha in _psql(container, "SELECT version_num FROM alembic_version ORDER BY 1")
        ]

    tabelas = [
        linha[0]
        for linha in _psql(
            container,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
        )
    ]
    contagens: dict[str, int] = {}
    for tabela in tabelas:
        citada = '"' + tabela.replace('"', '""') + '"'
        contagens[tabela] = int(_escalar(container, f"SELECT count(*) FROM public.{citada}"))

    distribuicao: list[tuple[str, int]] = []
    acima_do_maximo: int | None = None
    if _tabela_existe(container, "configuracoes"):
        distribuicao = [
            (valor or "(nulo)", int(quantas))
            for valor, quantas in _psql(
                container,
                "SELECT tolerancia_dias_default, count(*) FROM configuracoes "
                "GROUP BY tolerancia_dias_default ORDER BY tolerancia_dias_default",
            )
        ]
        acima_do_maximo = int(
            _escalar(
                container,
                "SELECT count(*) FROM configuracoes "
                f"WHERE tolerancia_dias_default > {TOLERANCIA_DIAS_MAXIMA}",
            )
        )

    column_default = (
        _escalar(
            container,
            "SELECT coalesce(column_default, '(nulo)') FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'configuracoes' "
            "AND column_name = 'tolerancia_dias_default'",
        )
        or "(coluna ausente)"
    )
    constraint_presente = (
        _escalar(
            container,
            f"SELECT count(*) FROM pg_constraint WHERE conname = '{CONSTRAINT_TOLERANCIA}' "
            "AND contype = 'c'",
        )
        != "0"
    )
    return Inventario(
        versoes_alembic=versoes,
        contagens=contagens,
        distribuicao_tolerancia=distribuicao,
        column_default=column_default,
        acima_do_maximo=acima_do_maximo,
        constraint_presente=constraint_presente,
    )


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Roda o alembic do repositório contra `url`, sempre pela guarda.

    DATABASE_URL vai no env do subprocesso porque é assim que ela vence o `.env`
    do repositório no pydantic-settings (ver docstring do módulo).
    """
    exigir_banco_local(url)
    env = {**os.environ, "DATABASE_URL": url}
    return _rodar([sys.executable, "-m", "alembic", *args], cwd=RAIZ, env=env)


def _migracao_em_curso(saida: str) -> str | None:
    """Última revisão que o alembic anunciou antes de parar — a que quebrou."""
    anunciadas = re.findall(r"Running upgrade\s+(\w*)\s*->\s*(\w+)", saida)
    return anunciadas[-1][1] if anunciadas else None


@dataclass
class Conferencia:
    nome: str
    passou: bool
    detalhe: str


def conferir(antes: Inventario, depois: Inventario, heads: list[str]) -> list[Conferencia]:
    """Compara os dois retratos e devolve a lista de conferências do relatório."""
    conferencias: list[Conferencia] = []

    conferencias.append(
        Conferencia(
            "`alembic_version` igual ao `alembic heads`",
            sorted(depois.versoes_alembic) == sorted(heads),
            f"depois: {depois.versoes_alembic or ['(vazio)']} · heads: {heads or ['(vazio)']}",
        )
    )

    mantidas = [t for t in antes.contagens if t in depois.contagens]
    divergentes = [t for t in mantidas if antes.contagens[t] != depois.contagens[t]]
    conferencias.append(
        Conferencia(
            "tabelas que já existiam mantiveram a contagem",
            not divergentes,
            (
                f"{len(mantidas)} tabelas conferidas, nenhuma mudou"
                if not divergentes
                else "mudaram: "
                + ", ".join(
                    f"{t} {antes.contagens[t]} → {depois.contagens[t]}" for t in divergentes
                )
            ),
        )
    )

    novas = [t for t in depois.contagens if t not in antes.contagens]
    novas_com_linha = [t for t in novas if depois.contagens[t] != 0]
    conferencias.append(
        Conferencia(
            "tabelas criadas pela migração estão vazias",
            not novas_com_linha,
            (
                ("novas: " + ", ".join(novas) if novas else "a migração não criou tabela")
                if not novas_com_linha
                else "com linha: "
                + ", ".join(f"{t} ({depois.contagens[t]})" for t in novas_com_linha)
            ),
        )
    )

    sumidas = [t for t in antes.contagens if t not in depois.contagens]
    conferencias.append(
        Conferencia(
            "nenhuma tabela desapareceu",
            not sumidas,
            "sumiram: " + ", ".join(sumidas) if sumidas else "nenhuma",
        )
    )

    conferencias.append(
        Conferencia(
            "`configuracoes.tolerancia_dias_default` com default 0",
            depois.column_default == "0",
            f"`column_default` = {depois.column_default}",
        )
    )
    conferencias.append(
        Conferencia(
            f"nenhuma linha com tolerância acima de {TOLERANCIA_DIAS_MAXIMA}",
            depois.acima_do_maximo == 0,
            (
                f"{depois.acima_do_maximo} linha(s)"
                if depois.acima_do_maximo is not None
                else "tabela `configuracoes` ausente"
            ),
        )
    )
    conferencias.append(
        Conferencia(
            f"constraint `{CONSTRAINT_TOLERANCIA}` presente",
            depois.constraint_presente,
            "presente" if depois.constraint_presente else "ausente",
        )
    )
    return conferencias


def _tamanho_legivel(bytes_: int) -> str:
    valor = float(bytes_)
    for unidade in ("B", "KiB", "MiB", "GiB"):
        if valor < 1024 or unidade == "GiB":
            return f"{valor:.1f} {unidade}"
        valor /= 1024
    return f"{valor:.1f} GiB"


def _duracao_legivel(segundos: float) -> str:
    if segundos < 60:
        return f"{segundos:.1f}s"
    minutos, resto = divmod(int(segundos), 60)
    if minutos < 60:
        return f"{minutos}min{resto:02d}s"
    horas, minutos = divmod(minutos, 60)
    return f"{horas}h{minutos:02d}min"


def _tabela_markdown(cabecalho: list[str], linhas: list[list[str]]) -> list[str]:
    saida = ["| " + " | ".join(cabecalho) + " |", "|" + "---|" * len(cabecalho)]
    saida += ["| " + " | ".join(celula) + " |" for celula in linhas]
    return saida


def _distribuicao_markdown(antes: Inventario, depois: Inventario) -> list[str]:
    valores = sorted({v for v, _ in antes.distribuicao_tolerancia + depois.distribuicao_tolerancia})
    de_antes = dict(antes.distribuicao_tolerancia)
    de_depois = dict(depois.distribuicao_tolerancia)
    linhas = [
        [valor, str(de_antes.get(valor, 0)), str(de_depois.get(valor, 0))] for valor in valores
    ]
    if not linhas:
        return ["Tabela `configuracoes` sem linhas."]
    return _tabela_markdown(["tolerancia_dias_default", "linhas antes", "linhas depois"], linhas)


@dataclass
class Medidas:
    dump: Path
    pg_major: int
    rto: float
    rpo: float
    tempo_migracao: float
    saida_upgrade: str
    saida_check: str
    heads: list[str]


def montar_relatorio(antes: Inventario, depois: Inventario, medidas: Medidas) -> str:
    conferencias = conferir(antes, depois, medidas.heads)
    agora = datetime.now(UTC)
    nascimento_dump = datetime.fromtimestamp(medidas.dump.stat().st_mtime, UTC)

    linhas = [
        f"# Ensaio de restore + migração — {agora:%Y-%m-%d %H:%M} UTC",
        "",
        (
            "Gerado por `scripts/ensaio_restore.py` (issue #35). Restore de um dump do "
            "Postgres de produção num container descartável, seguido de `alembic upgrade "
            "head` e `alembic check` em cima da cópia (ensaio da issue #65). O banco de "
            "produção não é tocado em nenhum passo."
        ),
        "",
        "## Resumo",
        "",
    ]
    linhas += _tabela_markdown(
        ["item", "valor"],
        [
            ["arquivo de dump", f"`{medidas.dump.name}`"],
            ["tamanho do dump", _tamanho_legivel(medidas.dump.stat().st_size)],
            ["dump gerado em", f"{nascimento_dump:%Y-%m-%d %H:%M} UTC"],
            ["**RPO** (idade do dump no ensaio)", _duracao_legivel(medidas.rpo)],
            ["imagem do ensaio", f"`postgres:{medidas.pg_major}`"],
            ["**RTO** (tempo do `pg_restore`)", _duracao_legivel(medidas.rto)],
            ["tempo do `alembic upgrade head`", _duracao_legivel(medidas.tempo_migracao)],
            ["`alembic_version` antes", ", ".join(antes.versoes_alembic) or "(vazio)"],
            ["`alembic_version` depois", ", ".join(depois.versoes_alembic) or "(vazio)"],
            ["`alembic heads` do repositório", ", ".join(medidas.heads) or "(vazio)"],
        ],
    )

    linhas += ["", "## Conferências", ""]
    linhas += _tabela_markdown(
        ["conferência", "resultado", "detalhe"],
        [[c.nome, "OK" if c.passou else "FALHOU", c.detalhe] for c in conferencias],
    )

    linhas += ["", "## Linhas por tabela (schema `public`)", ""]
    todas = sorted(set(antes.contagens) | set(depois.contagens))
    linhas += _tabela_markdown(
        ["tabela", "antes", "depois", "situação"],
        [
            [
                f"`{tabela}`",
                str(antes.contagens.get(tabela, "—")),
                str(depois.contagens.get(tabela, "—")),
                _situacao_tabela(tabela, antes, depois),
            ]
            for tabela in todas
        ],
    )

    linhas += ["", "## Distribuição de `configuracoes.tolerancia_dias_default`", ""]
    linhas += _distribuicao_markdown(antes, depois)
    linhas += [
        "",
        (
            f"`column_default` antes: {antes.column_default} · depois: "
            f"{depois.column_default} (esperado 0). Linhas acima do máximo da API "
            f"({TOLERANCIA_DIAS_MAXIMA}): {depois.acima_do_maximo} (esperado 0). "
            f"Constraint `{CONSTRAINT_TOLERANCIA}`: "
            + ("presente." if depois.constraint_presente else "ausente.")
        ),
        "",
        "## Saída do alembic",
        "",
        "```",
        medidas.saida_upgrade.strip() or "(sem saída)",
        "```",
        "",
        "`alembic check`:",
        "",
        "```",
        medidas.saida_check.strip() or "(sem saída)",
        "```",
        "",
    ]
    todas_passaram = all(c.passou for c in conferencias)
    linhas.append(
        "**Resultado: ensaio concluído, todas as conferências passaram.**"
        if todas_passaram
        else "**Resultado: ensaio com conferência reprovada — ver a tabela acima.**"
    )
    return "\n".join(linhas) + "\n"


def _situacao_tabela(tabela: str, antes: Inventario, depois: Inventario) -> str:
    if tabela not in antes.contagens:
        return "criada pela migração"
    if tabela not in depois.contagens:
        return "removida pela migração"
    if antes.contagens[tabela] == depois.contagens[tabela]:
        return "igual"
    return "MUDOU"


def _argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ensaia o restore do backup do Postgres de produção num container "
            "descartável e roda a migração pendente em cima da cópia (issue #35). "
            "Nunca toca o banco de produção."
        )
    )
    parser.add_argument("--dump", required=True, type=Path, help="dump no formato custom")
    parser.add_argument(
        "--pg-major",
        type=int,
        default=18,
        help="major do Postgres do container (padrão 18, o mesmo do Railway)",
    )
    parser.add_argument(
        "--porta", type=int, default=55432, help="porta publicada em 127.0.0.1 (padrão 55432)"
    )
    parser.add_argument(
        "--relatorio",
        type=Path,
        default=None,
        help=(
            "caminho do relatório markdown, fora do repositório (padrão: ao lado do dump). "
            "O relatório leva só contagens e nomes de tabela, nenhum dado de cliente."
        ),
    )
    parser.add_argument(
        "--manter",
        action="store_true",
        help="não apaga o container no fim, pra inspecionar a cópia restaurada",
    )
    return parser.parse_args()


def _destino_do_relatorio(args: argparse.Namespace, dump: Path) -> Path:
    if args.relatorio is not None:
        destino = args.relatorio.expanduser().resolve()
    else:
        agora = datetime.now(UTC)
        destino = dump.parent / f"ensaio-restore-{agora:%Y%m%d-%H%M}.md"
    if destino.is_relative_to(RAIZ):
        raise SystemExit(
            f"O relatório iria pra dentro do repositório ({destino}). Escolha um "
            f"caminho fora dele em --relatorio: nada do ensaio entra no git."
        )
    return destino


def main() -> int:
    args = _argumentos()
    dump = args.dump.expanduser().resolve()
    if not dump.is_file():
        raise SystemExit(
            f"Dump não encontrado em {dump}. O dump é gerado por uma pessoa, com o "
            f"comando do roteiro em 07-tecnico/backup-e-restore.md (no vault)."
        )
    relatorio = _destino_do_relatorio(args, dump)

    senha = secrets.token_hex(16)
    url = f"postgresql+psycopg://{USUARIO_PG}:{senha}@127.0.0.1:{args.porta}/{BANCO_ENSAIO}"
    # Antes de qualquer coisa: se a guarda recusar esta URL, o ensaio não começa.
    exigir_banco_local(url)
    url_mascarada = url.replace(senha, "***")

    print(f"Dump:      {dump}")
    print(f"Container: postgres:{args.pg_major} em 127.0.0.1:{args.porta}")
    print(f"Conexão:   {url_mascarada}")

    container = subir_container(args.pg_major, args.porta, senha)
    manter = args.manter
    try:
        print(f"Container {container} criado; esperando o Postgres ficar pronto...")
        esperar_banco(container, args.porta)

        rpo = time.time() - dump.stat().st_mtime
        print("Restaurando o dump...")
        rto = restaurar(container, dump)
        print(f"Restore concluído em {_duracao_legivel(rto)} (RTO).")

        antes = coletar_inventario(container)
        print(f"Antes da migração: alembic_version = {antes.versoes_alembic or ['(vazio)']}")

        print("Rodando `alembic upgrade head`...")
        inicio = time.monotonic()
        upgrade = _alembic(url, "upgrade", "head")
        tempo_migracao = time.monotonic() - inicio
        saida_upgrade = (upgrade.stdout + upgrade.stderr).replace(senha, "***")
        if upgrade.returncode != 0:
            quebrou = _migracao_em_curso(saida_upgrade)
            manter = True
            raise EnsaioFalhou(
                f"`alembic upgrade head` falhou na migração "
                f"{quebrou or '(não identificada na saída)'}:\n{saida_upgrade.strip()}"
            )

        check = _alembic(url, "check")
        saida_check = (check.stdout + check.stderr).replace(senha, "***")
        if check.returncode != 0:
            manter = True
            raise EnsaioFalhou(f"`alembic check` acusou drift:\n{saida_check.strip()}")

        heads = _alembic(url, "heads")
        revisoes_head = re.findall(r"^(\w+)", heads.stdout.strip(), flags=re.MULTILINE)

        depois = coletar_inventario(container)
        medidas = Medidas(
            dump=dump,
            pg_major=args.pg_major,
            rto=rto,
            rpo=rpo,
            tempo_migracao=tempo_migracao,
            saida_upgrade=saida_upgrade,
            saida_check=saida_check,
            heads=revisoes_head,
        )
        relatorio.parent.mkdir(parents=True, exist_ok=True)
        relatorio.write_text(montar_relatorio(antes, depois, medidas), encoding="utf-8")

        conferencias = conferir(antes, depois, revisoes_head)
        print()
        for conferencia in conferencias:
            print(f"[{'OK' if conferencia.passou else 'FALHOU'}] {conferencia.nome}")
            print(f"         {conferencia.detalhe}")
        print()
        print(f"RTO (restore): {_duracao_legivel(rto)}")
        print(f"RPO (idade do dump): {_duracao_legivel(rpo)}")
        print(f"Relatório: {relatorio}")
        if all(c.passou for c in conferencias):
            return 0
        print("Alguma conferência reprovou — ver o relatório.")
        manter = True
        return 1
    except EnsaioFalhou as erro:
        manter = True
        print(f"\nEnsaio falhou: {erro}", file=sys.stderr)
        return 1
    finally:
        if manter:
            print(
                f"\nContainer {container} mantido pra inspeção. Pra entrar:\n"
                f"  docker exec -it {container} psql -U {USUARIO_PG} -d {BANCO_ENSAIO}\n"
                f"Pra apagar:\n  docker rm -f {container}"
            )
        else:
            _rodar(["docker", "rm", "--force", "--volumes", container])
            print(f"Container {container} removido.")


if __name__ == "__main__":
    sys.exit(main())
