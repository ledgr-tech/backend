"""scripts/ativar_empresa.py (issue #122): marca uma empresa como assinante
ativa, ou desfaz, com confirmação pelo CNPJ e a guarda de banco remoto de
scripts/excluir_empresa.py.

Roda contra o Postgres local da suíte. O que o script pede digitado vem de
`perguntar`, trocado aqui por respostas fixas.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models import Empresa
from scripts import ativar_empresa as script

URL_REMOTA = "postgresql+psycopg://ledgr:segredo@banco.railway.example:5432/railway"


class _Respostas:
    """Faz o papel do `input`: devolve as respostas na ordem e guarda as perguntas."""

    def __init__(self, *respostas: str) -> None:
        self._respostas = list(respostas)
        self.perguntas: list[str] = []

    def __call__(self, pergunta: str) -> str:
        self.perguntas.append(pergunta)
        return self._respostas.pop(0)


def _com_mascara(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def _ativa(db_session, empresa) -> bool:
    db_session.expire_all()
    return db_session.get(Empresa, empresa.id).assinatura_ativa


def test_ativa_e_desativa_com_o_cnpj_certo(db_session, criar_empresa, capsys):
    empresa = criar_empresa("Piloto Contabilidade Ltda")

    ativou = script.main(
        ["--empresa-id", str(empresa.id), "--ativo", "true"],
        perguntar=_Respostas(_com_mascara(empresa.cnpj)),
    )
    saida_ativar = capsys.readouterr().out
    assert ativou == 0
    assert _ativa(db_session, empresa) is True

    desativou = script.main(
        ["--empresa-id", str(empresa.id), "--ativo", "false"],
        perguntar=_Respostas(empresa.cnpj),
    )
    saida_desativar = capsys.readouterr().out
    assert desativou == 0
    assert _ativa(db_session, empresa) is False

    assert "Piloto Contabilidade Ltda" in saida_ativar
    assert f"{empresa.cnpj[:2]}.***.***/****-{empresa.cnpj[-2:]}" in saida_ativar
    assert empresa.cnpj not in saida_ativar
    assert "Situação atual:  sem_plano" in saida_ativar
    assert "Situação nova: ativo" in saida_ativar
    assert "Situação atual:  ativo" in saida_desativar
    assert "Situação nova: sem_plano" in saida_desativar


def test_desativar_volta_para_a_situacao_do_teste(db_session, criar_empresa, capsys):
    empresa = criar_empresa()
    empresa.plano = "padrao"
    empresa.teste_termina_em = datetime.now(UTC) - timedelta(days=1)
    empresa.assinatura_ativa = True
    db_session.commit()

    codigo = script.main(
        ["--empresa-id", str(empresa.id), "--ativo", "false"],
        perguntar=_Respostas(empresa.cnpj),
    )

    assert codigo == 0
    assert "Situação nova: teste_encerrado" in capsys.readouterr().out


def test_cnpj_errado_nao_grava(db_session, criar_empresa, capsys):
    empresa = criar_empresa()

    codigo = script.main(
        ["--empresa-id", str(empresa.id), "--ativo", "true"],
        perguntar=_Respostas("12.345.678/0001-95"),
    )

    assert codigo == 1
    assert _ativa(db_session, empresa) is False
    assert "Nada foi gravado." in capsys.readouterr().out


def test_empresa_inexistente(db_session, capsys):
    perguntar = _Respostas()

    codigo = script.main(
        ["--empresa-id", str(uuid.uuid4()), "--ativo", "true"], perguntar=perguntar
    )

    assert codigo == 1
    assert perguntar.perguntas == []
    assert "não encontrada" in capsys.readouterr().out


@pytest.mark.parametrize("argumentos", [[], ["--ativo", "sim"], ["--ativo", "True"]])
def test_ativo_e_obrigatorio_e_so_aceita_true_ou_false(argumentos):
    with pytest.raises(SystemExit):
        script.main(["--empresa-id", str(uuid.uuid4()), *argumentos], perguntar=_Respostas())


# --- banco remoto ---------------------------------------------------------------


class _ConectouNoRemoto(Exception):
    pass


@pytest.fixture
def banco_remoto(monkeypatch):
    """DATABASE_URL remota, e nenhuma conexão: se o script passar da guarda,
    `_criar_engine` levanta `_ConectouNoRemoto`."""
    monkeypatch.setattr(settings, "database_url", URL_REMOTA)
    tentativas: list[str] = []

    def _criar_engine(url):
        tentativas.append(url)
        raise _ConectouNoRemoto

    monkeypatch.setattr(script, "_criar_engine", _criar_engine)
    return tentativas


def test_banco_remoto_sem_a_flag_e_recusado(banco_remoto, capsys):
    perguntar = _Respostas()

    codigo = script.main(
        ["--empresa-id", str(uuid.uuid4()), "--ativo", "true"], perguntar=perguntar
    )

    assert codigo == 1
    assert banco_remoto == []
    assert perguntar.perguntas == []
    saida = capsys.readouterr().out
    assert "Recusado" in saida
    assert "segredo" not in saida


def test_banco_remoto_com_a_flag_e_o_host_errado_e_recusado(banco_remoto, capsys):
    perguntar = _Respostas("localhost")

    codigo = script.main(
        ["--empresa-id", str(uuid.uuid4()), "--ativo", "true", "--banco-remoto"],
        perguntar=perguntar,
    )

    assert codigo == 1
    assert banco_remoto == []
    assert len(perguntar.perguntas) == 1
    assert "Nada foi feito." in capsys.readouterr().out


def test_banco_remoto_com_a_flag_e_o_host_digitado_passa_da_guarda(banco_remoto):
    with pytest.raises(_ConectouNoRemoto):
        script.main(
            ["--empresa-id", str(uuid.uuid4()), "--ativo", "true", "--banco-remoto"],
            perguntar=_Respostas("banco.railway.example"),
        )

    assert banco_remoto == [URL_REMOTA]
