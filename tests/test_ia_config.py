"""Testes da configuração de IA (issue #28, ADR-011) e da fábrica
`obter_provedor` — sem banco, sem rede. `Settings` só lê variáveis de
ambiente/`.env`, nunca conecta em nada; `obter_provedor` só decide qual
implementação instanciar, sem chamar a API.
"""

import os
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings
from app.services.ia import ProvedorOpenAI, obter_provedor

RAIZ = Path(__file__).resolve().parent.parent

_VARIAVEIS_LLM = (
    "LLM_HABILITADO",
    "LLM_PROVEDOR",
    "OPENAI_API_KEY",
    "OPENAI_MODELO",
    "OPENAI_BASE_URL",
    "LLM_TIMEOUT_SEGUNDOS",
    "LLM_MAX_TOKENS_SAIDA",
)


def _settings_sem_variaveis_de_ambiente(monkeypatch, **overrides) -> Settings:
    for variavel in _VARIAVEIS_LLM:
        monkeypatch.delenv(variavel, raising=False)
    return Settings(
        database_url="postgresql+psycopg://usuario:senha@localhost:5432/ledgr",
        nextauth_secret="",
        **overrides,
    )


def test_defaults_de_llm_sem_variaveis_de_ambiente(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(monkeypatch)

    assert config.llm_habilitado is False
    assert config.llm_provedor == "openai"
    assert config.openai_api_key == ""
    assert config.openai_modelo == "gpt-6-luna"
    assert config.openai_base_url == "https://api.openai.com/v1"
    assert config.llm_timeout_segundos == 20.0
    assert config.llm_max_tokens_saida == 300


def test_ausencia_das_variaveis_nao_quebra_a_aplicacao(monkeypatch):
    # Settings() não levanta exceção mesmo sem nenhuma variável de LLM
    # configurada — só database_url/nextauth_secret são obrigatórios.
    _settings_sem_variaveis_de_ambiente(monkeypatch)


# obter_provedor


def test_obter_provedor_none_quando_desabilitado(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(
        monkeypatch, llm_habilitado=False, openai_api_key="sk-teste"
    )

    assert obter_provedor(config) is None


def test_obter_provedor_none_sem_chave(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(
        monkeypatch, llm_habilitado=True, openai_api_key=""
    )

    assert obter_provedor(config) is None


def test_obter_provedor_none_com_provedor_desconhecido(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(
        monkeypatch, llm_habilitado=True, openai_api_key="sk-teste", llm_provedor="maritaca"
    )

    assert obter_provedor(config) is None


def test_obter_provedor_instancia_quando_tudo_configurado(monkeypatch):
    config = _settings_sem_variaveis_de_ambiente(
        monkeypatch,
        llm_habilitado=True,
        openai_api_key="sk-teste",
        llm_provedor="openai",
        openai_modelo="gpt-6-luna",
        openai_base_url="https://api.openai.com/v1",
        llm_timeout_segundos=20.0,
        llm_max_tokens_saida=300,
    )

    provedor = obter_provedor(config)

    assert isinstance(provedor, ProvedorOpenAI)


# import preguiçoso de settings (issue #28): importar app.services.ia não
# pode exigir DATABASE_URL nem .env, já que nada nesse pacote toca banco —
# só obter_provedor(), quando de fato chamado sem settings explícito, lê a
# configuração (import lazy dentro da função).


def test_importar_camada_de_ia_nao_exige_database_url_nem_env(tmp_path):
    """Roda num subprocesso, com cwd num diretório sem `.env` e sem
    `DATABASE_URL` no ambiente, pra garantir que nada em
    app.services.ia.{base,openai_provider,prompt} importa app.core.config
    no nível do módulo."""
    ambiente_limpo = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(RAIZ)}
    codigo = (
        "import app.services.ia.base\n"
        "import app.services.ia.openai_provider\n"
        "import app.services.ia.prompt\n"
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
