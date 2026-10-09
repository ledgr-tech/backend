"""Saída dos logs de `app/` (issue #102) e regra da issue #34 nos logs.

Antes da #102 nada configurava o logger `app`: o uvicorn só configura os
loggers dele, e o Python descartava todo INFO de `app.*`. Os testes de saída
rodam num subprocesso que importa `main`, como o uvicorn faz, porque dentro do
pytest a captura de stderr e o conftest (que religa a propagação para o
`caplog`) mudariam justamente o que está sendo testado.
"""

import logging
import os
import subprocess
import sys

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
