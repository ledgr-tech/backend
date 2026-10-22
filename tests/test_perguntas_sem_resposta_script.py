"""scripts/perguntas_sem_resposta.py (issue #137): lista as perguntas que a
Central de ajuda não respondeu, por tela, com a guarda de banco remoto dos
outros scripts. Roda contra o Postgres local da suíte."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.models import PerguntaSemResposta
from scripts import perguntas_sem_resposta as script

URL_REMOTA = "postgresql+psycopg://ledgr:segredo@banco.railway.example:5432/railway"


@pytest.fixture
def perguntas(db_session):
    marca = uuid.uuid4().hex[:8]
    agora = datetime.now(UTC)
    linhas = [
        PerguntaSemResposta(
            pergunta=f"{marca} como emito boleto?", tela="extratos", criado_em=agora
        ),
        PerguntaSemResposta(
            pergunta=f"{marca} posso importar PDF?",
            tela="extratos",
            criado_em=agora - timedelta(days=2),
        ),
        PerguntaSemResposta(pergunta=f"{marca} tem app?", tela=None, criado_em=agora),
        PerguntaSemResposta(
            pergunta=f"{marca} antiga", tela="fechamento", criado_em=agora - timedelta(days=40)
        ),
    ]
    db_session.add_all(linhas)
    db_session.commit()
    yield marca
    db_session.execute(
        delete(PerguntaSemResposta).where(PerguntaSemResposta.pergunta.like(f"{marca}%"))
    )
    db_session.commit()


def test_lista_as_dos_ultimos_dias_por_tela(db_session, perguntas, capsys):
    assert script.main(["--dias", "30"], perguntar=lambda _: "") == 0

    saida = capsys.readouterr().out
    assert "extratos (" in saida and script.SEM_TELA in saida
    assert f"{perguntas} como emito boleto?" in saida
    assert f"{perguntas} posso importar PDF?" in saida
    assert f"{perguntas} antiga" not in saida
    # As da tela extratos vêm juntas, a mais recente primeiro.
    assert saida.index(f"{perguntas} como emito boleto?") < saida.index(
        f"{perguntas} posso importar PDF?"
    )


def test_dias_inclui_as_mais_antigas(db_session, perguntas, capsys):
    script.main(["--dias", "60"], perguntar=lambda _: "")

    assert f"{perguntas} antiga" in capsys.readouterr().out


@pytest.mark.parametrize("dias", ["0", "91", "-3"])
def test_dias_fora_de_1_a_90_e_recusado(dias):
    with pytest.raises(SystemExit):
        script.main(["--dias", dias], perguntar=lambda _: "")


def test_banco_remoto_sem_a_flag_e_recusado(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", URL_REMOTA)

    def _nao_conecta(url):
        raise AssertionError("conectou no remoto")

    monkeypatch.setattr(script, "_criar_engine", _nao_conecta)

    assert script.main([], perguntar=lambda _: "") == 1
    saida = capsys.readouterr().out
    assert "Recusado" in saida and "segredo" not in saida
