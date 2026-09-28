"""Testes da configuração de e-mail (issue #66, ADR-012) e da fábrica
`obter_provedor_email` — sem banco, sem rede. Mesmo esquema de
tests/test_ia_config.py.
"""

import os
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings
from app.services.email import ProvedorResend, obter_provedor_email

RAIZ = Path(__file__).resolve().parent.parent

_VARIAVEIS_EMAIL = (
    "EMAIL_HABILITADO",
    "EMAIL_PROVEDOR",
    "RESEND_API_KEY",
    "RESEND_BASE_URL",
    "EMAIL_REMETENTE",
    "EMAIL_RESPONDER_PARA",
    "EMAIL_TIMEOUT_SEGUNDOS",
    "FRONTEND_URL",
)


def _settings_sem_variaveis_de_ambiente(monkeypatch, **overrides) -> Settings:
    for variavel in _VARIAVEIS_EMAIL:
        monkeypatch.delenv(variavel, raising=False)
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://usuario:senha@localhost:5432/ledgr",
        nextauth_secret="",
        **overrides,
    )


def _settings_completas(monkeypatch, **overrides) -> Settings:
    valores = {
        "email_habilitado": True,
        "email_provedor": "resend",
        "resend_api_key": "re_teste",
        "email_remetente": "Ledgr <nao-responda@conta.ledgr.com.br>",
        **overrides,
    }
    return _settings_sem_variaveis_de_ambiente(monkeypatch, **valores)


def test_defaults_de_email_sem_variaveis_de_ambiente(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(monkeypatch)

    assert config.email_habilitado is False
    assert config.email_provedor == "resend"
    assert config.resend_api_key == ""
    assert config.resend_base_url == "https://api.resend.com"
    assert config.email_remetente == ""
    assert config.email_responder_para == ""
    assert config.email_timeout_segundos == 10.0
    assert config.frontend_url == ""


# obter_provedor_email


def test_obter_provedor_email_none_quando_desabilitado(monkeypatch):
    config = _settings_completas(monkeypatch, email_habilitado=False)

    assert obter_provedor_email(config) is None


def test_obter_provedor_email_none_sem_chave(monkeypatch):
    config = _settings_completas(monkeypatch, resend_api_key="  ")

    assert obter_provedor_email(config) is None


def test_obter_provedor_email_none_sem_remetente(monkeypatch):
    config = _settings_completas(monkeypatch, email_remetente="")

    assert obter_provedor_email(config) is None


def test_obter_provedor_email_none_com_provedor_desconhecido(monkeypatch):
    config = _settings_completas(monkeypatch, email_provedor="ses")

    assert obter_provedor_email(config) is None


def test_obter_provedor_email_instancia_quando_tudo_configurado(monkeypatch):
    config = _settings_completas(monkeypatch)

    assert isinstance(obter_provedor_email(config), ProvedorResend)


def test_importar_camada_de_email_nao_exige_database_url_nem_env(tmp_path):
    """Mesmo cuidado de tests/test_ia_config.py: nada em
    app.services.email importa app.core.config no nível do módulo."""
    ambiente_limpo = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(RAIZ)}
    codigo = (
        "import app.services.email\n"
        "import app.services.email.base\n"
        "import app.services.email.resend_provider\n"
        "import app.services.email.mensagens\n"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=tmp_path,
        env=ambiente_limpo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert resultado.returncode == 0, resultado.stderr
