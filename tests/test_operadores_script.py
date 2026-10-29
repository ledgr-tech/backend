"""scripts/operadores.py (issue #156, ADR-025): inclui, remove e lista os
operadores do Ledgr, com o e-mail digitado como confirmação e a guarda de
banco remoto de scripts/excluir_empresa.py. Roda contra o Postgres local da
suíte; o que o script pede digitado vem de `perguntar`."""

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import OperadorLedgr
from scripts import operadores as script

URL_REMOTA = "postgresql+psycopg://ledgr:segredo@banco.railway.example:5432/railway"


class _Respostas:
    def __init__(self, *respostas: str) -> None:
        self._respostas = list(respostas)
        self.perguntas: list[str] = []

    def __call__(self, pergunta: str) -> str:
        self.perguntas.append(pergunta)
        return self._respostas.pop(0)


def _ativos(db_session, usuario) -> list[OperadorLedgr]:
    db_session.expire_all()
    return db_session.scalars(
        select(OperadorLedgr).where(
            OperadorLedgr.usuario_id == usuario.id, OperadorLedgr.removido_em.is_(None)
        )
    ).all()


def test_incluir_com_o_email_certo(db_session, criar_usuario, capsys):
    usuario = criar_usuario(nome="Olga Operadora")

    codigo = script.main(
        ["--incluir", usuario.email.upper(), "--por", "Rafael"],
        perguntar=_Respostas(f"  {usuario.email}  "),
    )

    saida = capsys.readouterr().out
    assert codigo == 0
    (operador,) = _ativos(db_session, usuario)
    assert operador.incluido_por == "Rafael"
    assert usuario.email not in saida
    assert script.email_mascarado(usuario.email) in saida
    assert "Olga" not in saida


def test_email_errado_nao_grava(db_session, criar_usuario, capsys):
    usuario = criar_usuario()

    codigo = script.main(
        ["--incluir", usuario.email, "--por", "Rafael"], perguntar=_Respostas("outro@x.com")
    )

    assert codigo == 1
    assert _ativos(db_session, usuario) == []
    assert "não confere" in capsys.readouterr().out


def test_incluir_de_novo_e_usuario_inexistente(db_session, criar_usuario, capsys):
    usuario = criar_usuario()
    script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas(usuario.email))

    repetido = script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas())
    inexistente = script.main(
        ["--incluir", "ninguem@teste.com", "--por", "R"], perguntar=_Respostas()
    )

    assert (repetido, inexistente) == (1, 1)
    assert len(_ativos(db_session, usuario)) == 1


def test_remover_e_incluir_de_novo(db_session, criar_usuario, capsys):
    usuario = criar_usuario()
    script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas(usuario.email))

    removeu = script.main(
        ["--remover", usuario.email, "--por", "Marta"], perguntar=_Respostas(usuario.email)
    )

    assert removeu == 0
    assert _ativos(db_session, usuario) == []
    removido = db_session.scalar(
        select(OperadorLedgr).where(OperadorLedgr.usuario_id == usuario.id)
    )
    assert (removido.removido_por, removido.removido_em is not None) == ("Marta", True)
    assert script.main(["--remover", usuario.email, "--por", "M"], perguntar=_Respostas()) == 1
    # Incluir de novo cria outra linha; o histórico fica.
    script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas(usuario.email))
    assert len(_ativos(db_session, usuario)) == 1


def test_listar_mascara_o_email(db_session, criar_usuario, capsys):
    usuario = criar_usuario()
    script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas(usuario.email))
    capsys.readouterr()

    assert script.main(["--listar"], perguntar=_Respostas()) == 0

    saida = capsys.readouterr().out
    assert script.email_mascarado(usuario.email) in saida
    assert usuario.email not in saida


def test_incluir_e_remover_pedem_quem_rodou():
    with pytest.raises(SystemExit):
        script.main(["--incluir", "a@b.com"], perguntar=_Respostas())


def test_banco_remoto_sem_a_opcao_e_recusado(db_session, criar_usuario, monkeypatch, capsys):
    usuario = criar_usuario()
    monkeypatch.setattr(settings, "database_url", URL_REMOTA)

    sem_opcao = script.main(["--incluir", usuario.email, "--por", "R"], perguntar=_Respostas())
    host_errado = script.main(
        ["--incluir", usuario.email, "--por", "R", "--banco-remoto"],
        perguntar=_Respostas("outro.host"),
    )

    assert (sem_opcao, host_errado) == (1, 1)
    assert "Recusado" in capsys.readouterr().out
    assert _ativos(db_session, usuario) == []
