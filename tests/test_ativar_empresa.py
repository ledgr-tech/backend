"""scripts/ativar_empresa.py (issues #122 e #133): marca uma conta como
assinante ativa, ou desfaz, pelo id ou pelo CNPJ da conta, com confirmação
pelo CNPJ e a guarda de banco remoto de scripts/excluir_empresa.py.

Roda contra o Postgres local da suíte. O que o script pede digitado vem de
`perguntar`, trocado aqui por respostas fixas.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models import Conta
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


def _ativa(db_session, conta) -> bool:
    db_session.expire_all()
    return db_session.get(Conta, conta.id).assinatura_ativa


@pytest.fixture
def criar_conta_com_cliente(db_session, criar_empresa):
    """Conta com a primeira empresa e uma empresa cliente: o script ativa a
    conta, não uma empresa (issue #133)."""

    def _criar(razao_social: str = "Empresa de teste") -> Conta:
        primeira = criar_empresa(razao_social)
        criar_empresa("Cliente", conta_id=primeira.conta_id)
        return db_session.get(Conta, primeira.conta_id)

    return _criar


def test_ativa_e_desativa_com_o_cnpj_certo(db_session, criar_conta_com_cliente, capsys):
    conta = criar_conta_com_cliente("Piloto Contabilidade Ltda")

    ativou = script.main(
        ["--conta", str(conta.id), "--ativo", "true"],
        perguntar=_Respostas(_com_mascara(conta.cnpj)),
    )
    saida_ativar = capsys.readouterr().out
    assert ativou == 0
    assert _ativa(db_session, conta) is True

    desativou = script.main(
        ["--conta", str(conta.id), "--ativo", "false"],
        perguntar=_Respostas(conta.cnpj),
    )
    saida_desativar = capsys.readouterr().out
    assert desativou == 0
    assert _ativa(db_session, conta) is False

    assert "Piloto Contabilidade Ltda" in saida_ativar
    assert f"{conta.cnpj[:2]}.***.***/****-{conta.cnpj[-2:]}" in saida_ativar
    assert conta.cnpj not in saida_ativar
    assert "Situação atual:  sem_plano" in saida_ativar
    assert "Situação nova: ativo" in saida_ativar
    assert "Situação atual:  ativo" in saida_desativar
    assert "Situação nova: sem_plano" in saida_desativar


@pytest.mark.parametrize("pelo", ["id", "cnpj", "cnpj_com_mascara", "empresa_id_antigo"])
def test_acha_a_conta_pelo_id_ou_pelo_cnpj(db_session, criar_conta_com_cliente, capsys, pelo):
    conta = criar_conta_com_cliente()
    argumentos = {
        "id": ["--conta", str(conta.id)],
        "cnpj": ["--conta", conta.cnpj],
        "cnpj_com_mascara": ["--conta", _com_mascara(conta.cnpj)],
        "empresa_id_antigo": ["--empresa-id", str(conta.id)],
    }[pelo]

    codigo = script.main([*argumentos, "--ativo", "true"], perguntar=_Respostas(conta.cnpj))

    assert codigo == 0
    assert _ativa(db_session, conta) is True
    assert f"Conta:           {conta.id}" in capsys.readouterr().out


def test_id_de_empresa_cliente_nao_e_conta(db_session, criar_empresa, capsys):
    primeira = criar_empresa()
    cliente = criar_empresa("Cliente", conta_id=primeira.conta_id)
    perguntar = _Respostas()

    codigo = script.main(["--conta", str(cliente.id), "--ativo", "true"], perguntar=perguntar)

    assert codigo == 1
    assert perguntar.perguntas == []
    assert "não encontrada" in capsys.readouterr().out


def test_desativar_volta_para_a_situacao_do_teste(db_session, criar_conta_com_cliente, capsys):
    conta = criar_conta_com_cliente()
    conta.plano = "padrao"
    conta.teste_termina_em = datetime.now(UTC) - timedelta(days=1)
    conta.assinatura_ativa = True
    db_session.commit()

    codigo = script.main(
        ["--conta", str(conta.id), "--ativo", "false"],
        perguntar=_Respostas(conta.cnpj),
    )

    assert codigo == 0
    assert "Situação nova: teste_encerrado" in capsys.readouterr().out


def test_cnpj_errado_nao_grava(db_session, criar_conta_com_cliente, capsys):
    conta = criar_conta_com_cliente()

    codigo = script.main(
        ["--conta", str(conta.id), "--ativo", "true"],
        perguntar=_Respostas("12.345.678/0001-95"),
    )

    assert codigo == 1
    assert _ativa(db_session, conta) is False
    assert "Nada foi gravado." in capsys.readouterr().out


@pytest.mark.parametrize("conta", [str(uuid.uuid4()), "11.222.333/0001-81", "texto"])
def test_conta_inexistente(db_session, capsys, conta):
    perguntar = _Respostas()

    codigo = script.main(["--conta", conta, "--ativo", "true"], perguntar=perguntar)

    assert codigo == 1
    assert perguntar.perguntas == []
    assert "não encontrada" in capsys.readouterr().out


@pytest.mark.parametrize("argumentos", [[], ["--ativo", "sim"], ["--ativo", "True"]])
def test_ativo_e_obrigatorio_e_so_aceita_true_ou_false(argumentos):
    with pytest.raises(SystemExit):
        script.main(["--conta", str(uuid.uuid4()), *argumentos], perguntar=_Respostas())


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

    codigo = script.main(["--conta", str(uuid.uuid4()), "--ativo", "true"], perguntar=perguntar)

    assert codigo == 1
    assert banco_remoto == []
    assert perguntar.perguntas == []
    saida = capsys.readouterr().out
    assert "Recusado" in saida
    assert "segredo" not in saida


def test_banco_remoto_com_a_flag_e_o_host_errado_e_recusado(banco_remoto, capsys):
    perguntar = _Respostas("localhost")

    codigo = script.main(
        ["--conta", str(uuid.uuid4()), "--ativo", "true", "--banco-remoto"],
        perguntar=perguntar,
    )

    assert codigo == 1
    assert banco_remoto == []
    assert len(perguntar.perguntas) == 1
    assert "Nada foi feito." in capsys.readouterr().out


def test_banco_remoto_com_a_flag_e_o_host_digitado_passa_da_guarda(banco_remoto):
    with pytest.raises(_ConectouNoRemoto):
        script.main(
            ["--conta", str(uuid.uuid4()), "--ativo", "true", "--banco-remoto"],
            perguntar=_Respostas("banco.railway.example"),
        )

    assert banco_remoto == [URL_REMOTA]


def test_mudanca_grava_o_evento_da_conta_com_origem_script(db_session, criar_conta_com_cliente):
    """Issue #156: o script grava o mesmo evento do painel, sem operador."""
    from sqlalchemy import select

    from app.models import EventoConta

    conta = criar_conta_com_cliente()

    script.main(
        ["--conta", str(conta.id), "--ativo", "true", "--motivo", "Piloto de novembro"],
        perguntar=_Respostas(conta.cnpj),
    )
    script.main(["--conta", str(conta.id), "--ativo", "true"], perguntar=_Respostas(conta.cnpj))

    eventos = db_session.scalars(select(EventoConta).where(EventoConta.conta_id == conta.id)).all()
    assert [(e.tipo, e.motivo, e.origem, e.operador_id) for e in eventos] == [
        ("piloto_ligado", "Piloto de novembro", "script", None)
    ]
