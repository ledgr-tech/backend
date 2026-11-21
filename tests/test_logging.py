"""Saída dos logs de `app/` (issue #102) e regra da issue #34 nos logs.

Antes da #102 nada configurava o logger `app`: o uvicorn só configura os
loggers dele, e o Python descartava todo INFO de `app.*`. Os testes de saída
rodam num subprocesso que importa `main`, como o uvicorn faz, porque dentro do
pytest a captura de stderr e o conftest (que religa a propagação para o
`caplog`) mudariam justamente o que está sendo testado.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import logging as logs_app
from app.core.database import engine
from app.core.logging import SemQueryNoAcesso, configurar_logs
from app.services import normalizacao

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MENSAGEM = "mensagem-de-teste-issue-102"
DADO_PESSOAL = "PIX JOAO DA SILVA"


def _rodar_com_main(codigo: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "import logging\nimport main\n" + codigo],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_logger_info_de_modulo_app_aparece_uma_vez_no_stderr():
    processo = _rodar_com_main(f"logging.getLogger('app.services.qualquer').info('{MENSAGEM}')")

    assert processo.returncode == 0, processo.stderr
    linhas = [linha for linha in processo.stderr.splitlines() if MENSAGEM in linha]
    assert len(linhas) == 1, processo.stderr
    # %(asctime)s %(levelname)s %(name)s %(message)s
    assert linhas[0].endswith(f" INFO app.services.qualquer {MENSAGEM}")


def test_log_level_controla_o_nivel():
    processo = _rodar_com_main(
        f"logging.getLogger('app.x').info('info-{MENSAGEM}')\n"
        f"logging.getLogger('app.x').warning('warning-{MENSAGEM}')",
        LOG_LEVEL="warning",
    )

    assert processo.returncode == 0, processo.stderr
    assert f"info-{MENSAGEM}" not in processo.stderr
    assert f"WARNING app.x warning-{MENSAGEM}" in processo.stderr


def test_nao_mexe_nos_loggers_do_uvicorn():
    processo = _rodar_com_main(
        "for nome in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):\n"
        "    lg = logging.getLogger(nome)\n"
        "    assert lg.handlers == [] and lg.propagate and lg.level == 0, nome\n"
        "assert logging.getLogger().handlers == []\n"
    )

    assert processo.returncode == 0, processo.stderr


def test_parser_invalido_nao_leva_conteudo_do_arquivo_ao_log(criar_extrato, caplog):
    """O ofxtools põe o OFX inteiro na mensagem do erro, MEMO incluído."""
    extrato = criar_extrato("ofx")
    conteudo = f"<OFX><STMTTRN><MEMO>{DADO_PESSOAL}<<<</OFX>".encode()

    with caplog.at_level(logging.INFO, logger="app"):
        normalizacao.normalizar_extrato(extrato.id, "ofx", conteudo)

    assert "conteúdo inválido classe=OFXInvalidoError" in caplog.text
    assert DADO_PESSOAL not in caplog.text


def test_falha_inesperada_loga_a_classe_e_nao_a_mensagem(criar_extrato, caplog, monkeypatch):
    """Erro de banco traz os parâmetros do INSERT na mensagem (descrições)."""

    def _falhar(formato, conteudo):
        raise RuntimeError(f"Failing row contains ({DADO_PESSOAL})")

    monkeypatch.setattr(normalizacao, "_parse", _falhar)
    extrato = criar_extrato("csv")

    with caplog.at_level(logging.INFO, logger="app"):
        normalizacao.normalizar_extrato(extrato.id, "csv", b"")

    assert "falha inesperada" in caplog.text
    assert "classe=RuntimeError" in caplog.text
    assert DADO_PESSOAL not in caplog.text
    assert all(registro.exc_info is None for registro in caplog.records)


# --- query string fora do log de acesso (issue #234) ----------------------------------

ACESSO = '%s - "%s %s HTTP/%s" %d'


def _registro_de_acesso(caminho, *, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=ACESSO,
        args=args if args is not None else ("127.0.0.1:5000", "GET", caminho, "1.1", 200),
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("caminho", "esperado"),
    [
        ("/admin/contas?busca=teste%40exemplo.com", "/admin/contas"),
        ("/admin/contas?busca=12.345.678%2F0001-90&limit=20", "/admin/contas"),
        ("/me?", "/me"),
        ("/admin/contas", "/admin/contas"),
        ("/", "/"),
        ("/extratos/abc%3Fdef", "/extratos/abc%3Fdef"),  # "?" do caminho vem codificado
    ],
)
def test_filtro_tira_a_query_e_mantem_o_resto_da_linha(caminho, esperado):
    registro = _registro_de_acesso(caminho)

    assert SemQueryNoAcesso().filter(registro) is True

    assert registro.getMessage() == f'127.0.0.1:5000 - "GET {esperado} HTTP/1.1" 200'
    assert registro.args[:2] == ("127.0.0.1:5000", "GET")
    assert registro.args[3:] == ("1.1", 200)


@pytest.mark.parametrize(
    "args",
    [(), ("so-um",), ("a", "b"), ("a", "b", 3, "d", 5), {"caminho": "/x?y=1"}, None],
    ids=["vazio", "um", "dois", "caminho_nao_texto", "dict", "sem_args"],
)
def test_filtro_nao_quebra_com_args_inesperados(args):
    registro = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "msg", (), None)
    registro.args = args  # direto: o construtor do LogRecord já recusa um dict assim
    antes = registro.args

    assert SemQueryNoAcesso().filter(registro) is True

    assert registro.args == antes


def test_configurar_logs_instala_o_filtro_uma_vez_so():
    acesso = logging.getLogger("uvicorn.access")
    configurar_logs()
    configurar_logs()

    assert [f for f in acesso.filters if isinstance(f, SemQueryNoAcesso)] != []
    assert len([f for f in acesso.filters if isinstance(f, SemQueryNoAcesso)]) == 1


def test_linha_de_acesso_sem_a_busca_no_stderr():
    """Como o uvicorn: logger `uvicorn.access` com o formato dele, depois de
    importar `main`."""
    processo = _rodar_com_main(
        "log = logging.getLogger('uvicorn.access')\n"
        "log.setLevel('INFO')\n"
        "log.addHandler(logging.StreamHandler())\n"  # o uvicorn real instala o dele
        "log.info('%s - \"%s %s HTTP/%s\" %d', '1.2.3.4:5', 'GET', "
        "'/admin/contas?busca=pessoa%40exemplo.com', '1.1', 401)"
    )

    assert processo.returncode == 0, processo.stderr
    assert "pessoa" not in processo.stderr + processo.stdout
    assert '1.2.3.4:5 - "GET /admin/contas HTTP/1.1" 401' in processo.stderr + processo.stdout


# --- erro não tratado sem a mensagem nem o traceback (issue #34) ---------------------

# Como o Postgres e o SQLAlchemy põem o dado na mensagem do erro: o DETAIL de
# uma violação de unicidade ou de NOT NULL e a linha `[parameters: ...]`.
MENSAGEM_COM_DADO = f"Failing row contains ({DADO_PESSOAL})"


def _falhar_com(exc: BaseException):
    raise exc


def _ler_json(texto: str):
    return json.loads(texto)


def _info_da_excecao(funcao, *args):
    try:
        funcao(*args)
    except Exception:  # noqa: BLE001 - o teste quer a exceção que vier
        return sys.exc_info()
    raise AssertionError("a função deveria ter falhado")


def _registro_de_erro(exc_info, msg="Exception in ASGI application\n") -> logging.LogRecord:
    """Como o uvicorn registra uma exceção da aplicação: `logger.error(msg,
    exc_info=exc)` no `uvicorn.error`."""
    return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 0, msg, (), exc_info)


def test_erro_nao_tratado_sai_sem_a_mensagem_nem_o_traceback():
    registro = _registro_de_erro(_info_da_excecao(_falhar_com, RuntimeError(MENSAGEM_COM_DADO)))

    assert logs_app.SemDetalheNoErro().filter(registro) is True

    linha = logging.Formatter().format(registro)
    assert DADO_PESSOAL not in linha
    assert "Traceback" not in linha
    assert registro.exc_info is None
    assert registro.getMessage().startswith("erro_nao_tratado classe=RuntimeError ")


def test_onde_e_a_ultima_linha_do_projeto_e_pula_a_biblioteca():
    """O `json.loads` falha dentro da biblioteca padrão; o `onde` é a linha
    do projeto que a chamou."""
    linha_do_loads = _ler_json.__code__.co_firstlineno + 1
    registro = _registro_de_erro(_info_da_excecao(_ler_json, "{" + DADO_PESSOAL))

    logs_app.SemDetalheNoErro().filter(registro)

    mensagem = registro.getMessage()
    assert "classe=JSONDecodeError" in mensagem
    assert mensagem.endswith(f"onde=tests/test_logging.py:{linha_do_loads} em _ler_json")


def test_traceback_ja_formatado_por_outro_handler_tambem_sai():
    registro = _registro_de_erro(_info_da_excecao(_falhar_com, RuntimeError(MENSAGEM_COM_DADO)))
    logging.Formatter().format(registro)  # guarda o traceback em `exc_text`
    assert DADO_PESSOAL in registro.exc_text

    logs_app.SemDetalheNoErro().filter(registro)

    assert DADO_PESSOAL not in logging.Formatter().format(registro)


def test_causa_do_erro_vai_pelo_nome_da_classe():
    def _converter():
        try:
            _falhar_com(KeyError(DADO_PESSOAL))
        except KeyError as erro:
            raise ValueError("conversão falhou") from erro

    registro = _registro_de_erro(_info_da_excecao(_converter))

    logs_app.SemDetalheNoErro().filter(registro)

    mensagem = registro.getMessage()
    assert "classe=ValueError causa=KeyError " in mensagem
    assert DADO_PESSOAL not in logging.Formatter().format(registro)


def test_registro_sem_excecao_passa_como_veio():
    registro = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 0, "Started server process [%d]", (123,), None
    )

    assert logs_app.SemDetalheNoErro().filter(registro) is True

    assert registro.getMessage() == "Started server process [123]"


def _erro_do_banco_com_o_dado() -> DBAPIError:
    with engine.connect() as conexao:
        try:
            conexao.execute(
                text("select 1 / 0 where cast(:dado as text) is not null"), {"dado": DADO_PESSOAL}
            )
        except DBAPIError as erro:
            return erro
    raise AssertionError("a consulta deveria ter falhado")


def test_erro_do_banco_sai_com_classe_causa_e_sqlstate(db_session):
    erro = _erro_do_banco_com_o_dado()
    registro = _registro_de_erro((type(erro), erro, erro.__traceback__))

    logs_app.SemDetalheNoErro().filter(registro)

    mensagem = registro.getMessage()
    assert "classe=DataError causa=DivisionByZero sqlstate=22012 " in mensagem
    assert DADO_PESSOAL not in logging.Formatter().format(registro)


def test_erro_do_banco_nao_traz_os_parametros_na_mensagem(db_session):
    """Defesa a mais (`hide_parameters`): sem ela, a mensagem do SQLAlchemy
    termina com `[parameters: {'dado': ...}]`, e qualquer `str(exc)` leva o
    dado."""
    assert DADO_PESSOAL not in str(_erro_do_banco_com_o_dado())


def test_erro_nao_tratado_no_stderr_como_o_uvicorn():
    """Como o uvicorn: ele configura os loggers dele com o `LOGGING_CONFIG`
    antes de importar o app, e registra a exceção no `uvicorn.error`."""
    processo = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                import logging, logging.config
                import uvicorn.config
                logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
                import main
                try:
                    raise RuntimeError({MENSAGEM_COM_DADO!r})
                except RuntimeError as exc:
                    logging.getLogger("uvicorn.error").error(
                        "Exception in ASGI application\\n", exc_info=exc
                    )
                """),
        ],
        cwd=RAIZ,
        env={k: v for k, v in os.environ.items() if k != "LOG_LEVEL"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    saida = processo.stderr + processo.stdout
    assert processo.returncode == 0, saida
    assert DADO_PESSOAL not in saida
    assert "Traceback" not in saida
    assert "erro_nao_tratado classe=RuntimeError" in saida
