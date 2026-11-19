"""Alerta por e-mail a cada ação de operador no `/admin` (issue #234).

As seis ações (inativar, reativar, piloto, cancelar, manter e cupom) mandam um
e-mail por destinatário de `ALERTAS_ADMIN_EMAILS`, depois do commit; a ação
recusada não manda; a falha do provedor, o e-mail desligado e a lista vazia
nunca mudam a resposta da ação; e nenhum log leva e-mail, nome nem motivo.
"""

import logging
import uuid

import pytest
import stripe
from conftest import FRONTEND_URL_TESTE, ProvedorEmailFalso
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from test_admin_assinaturas import _assinar

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.models import Conta, EventoConta
from app.services import alerta_admin
from app.services.email.mensagens import mensagem_alerta_admin
from main import app

client = TestClient(app)

LISTA = " equipe@ledgr.teste , Seguranca@ledgr.teste,, EQUIPE@ledgr.teste "
EQUIPE = {"equipe@ledgr.teste", "Seguranca@ledgr.teste"}


class ProvedorQueConfere(ProvedorEmailFalso):
    """Guarda quantas linhas de `eventos_conta` da conta outra conexão enxerga
    no momento do envio: só o que já tem commit aparece."""

    def __init__(self) -> None:
        super().__init__()
        self.conta_id: uuid.UUID | None = None
        self.eventos_ao_enviar: list[int] = []

    def enviar(self, mensagem) -> None:
        with SessionLocal() as outra:
            self.eventos_ao_enviar.append(
                outra.scalar(
                    select(func.count())
                    .select_from(EventoConta)
                    .where(EventoConta.conta_id == self.conta_id)
                )
            )
        super().enviar(mensagem)


@pytest.fixture
def alertas(monkeypatch):
    from app.api.senha import obter_provedor_email_dependencia

    falso = ProvedorQueConfere()
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: falso
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)
    monkeypatch.setattr(settings, "alertas_admin_emails", LISTA)
    yield falso
    app.dependency_overrides.pop(obter_provedor_email_dependencia, None)


@pytest.fixture(autouse=True)
def _sem_acessos_de_outros_testes(db_session):
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()
    yield
    db_session.execute(text("DELETE FROM acessos_admin"))
    db_session.commit()


def _post(headers, conta_id, caminho, **corpo):
    return client.post(f"/admin/contas/{conta_id}/{caminho}", headers=headers, json=corpo)


def _alertas_enviados(provedor) -> list:
    return [m for m in provedor.enviadas if m.para in EQUIPE]


def _inativar_no_banco(db_session, conta_id):
    db_session.execute(
        update(Conta)
        .where(Conta.id == conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="antes")
    )
    db_session.commit()


# --- as seis ações ------------------------------------------------------------------

MOTIVO_SEM = "sem motivo informado"


def _inativar(o, c, **_):
    return _post(o.headers, c.conta_id, "inativar", motivo="Inadimplente há 90 dias")


def _reativar(o, c, db_session, **_):
    _inativar_no_banco(db_session, c.conta_id)
    return _post(o.headers, c.conta_id, "reativar", motivo="Pagou a fatura")


def _piloto(o, c, **_):
    return _post(o.headers, c.conta_id, "piloto", ativo=True, motivo="Piloto de dezembro")


def _cancelar(o, c, db_session, stripe_falso, **_):
    _assinar(db_session, stripe_falso, c.conta_id)
    return _post(
        o.headers,
        c.conta_id,
        "assinatura/cancelar",
        quando="fim_do_periodo",
        motivo="Pediu por telefone",
    )


def _manter(o, c, db_session, stripe_falso, **_):
    _assinar(db_session, stripe_falso, c.conta_id, agendada=True)
    return _post(o.headers, c.conta_id, "assinatura/manter", motivo="Cliente voltou atrás")


def _cupom(o, c, stripe_falso, **_):
    from stripe_falso import cupom_da_stripe

    stripe_falso.cupons["cupom_20"] = cupom_da_stripe()
    return _post(o.headers, c.conta_id, "cupom", cupom_id="cupom_20")


ACOES = [
    ("inativar", _inativar, "Conta inativada", "Inadimplente há 90 dias"),
    ("reativar", _reativar, "Conta reativada", "Pagou a fatura"),
    ("piloto", _piloto, "Piloto ligado", "Piloto de dezembro"),
    ("cancelar", _cancelar, "Cancelamento da assinatura agendado", "Pediu por telefone"),
    ("manter", _manter, "Cancelamento da assinatura desfeito", "Cliente voltou atrás"),
    ("cupom", _cupom, "Cupom dado à conta: Cupom Vinte por cento", MOTIVO_SEM),
]


@pytest.mark.parametrize(
    ("chamar", "acao", "motivo"),
    [(f, a, m) for _, f, a, m in ACOES],
    ids=[i for i, *_ in ACOES],
)
def test_cada_acao_manda_um_email_por_destinatario_depois_do_commit(
    db_session, operador, cliente, stripe_falso, alertas, chamar, acao, motivo
):
    o = operador()
    c = cliente("Escritório Alvo Ltda", email="admin@alvo.com.br")
    alertas.conta_id = c.conta_id

    resposta = chamar(o, c, db_session=db_session, stripe_falso=stripe_falso)

    assert resposta.status_code == 200, resposta.text
    enviados = _alertas_enviados(alertas)
    # Um por destinatário, sem repetido (a lista tem um endereço em duas grafias).
    assert sorted(m.para for m in enviados) == sorted(EQUIPE)
    for m in enviados:
        for texto in (m.texto, m.html):
            assert o.usuario.nome in texto
            assert o.usuario.email in texto
            assert "Escritório Alvo Ltda" in texto
            assert str(c.conta_id) in texto
            assert acao in texto
            assert motivo in texto
        assert m.assunto.startswith("Ledgr admin: ")
        assert "Escritório Alvo Ltda" in m.assunto
        assert "America" not in m.texto and " às " in m.texto  # a hora, em São Paulo
    # No momento do envio, o evento da ação já tinha commit.
    assert alertas.eventos_ao_enviar and all(n >= 1 for n in alertas.eventos_ao_enviar)
    # A chave de idempotência é uma por destinatário.
    assert len({m.chave_idempotencia for m in enviados}) == len(enviados)


def test_o_alerta_nao_vai_para_o_cliente(db_session, operador, cliente, alertas):
    o = operador()
    c = cliente(email="admin@alvo.com.br")

    _inativar(o, c)

    assert "admin@alvo.com.br" not in {m.para for m in alertas.enviadas}


def test_cancelar_na_hora_diz_que_foi_na_hora(db_session, operador, cliente, stripe_falso, alertas):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)

    resposta = _post(
        o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="Encerrou a empresa"
    )

    assert resposta.status_code == 200
    primeiro, _ = sorted(_alertas_enviados(alertas), key=lambda m: m.para)
    assert "Assinatura cancelada na hora" in primeiro.texto


def test_piloto_desligado_diz_desligado(db_session, operador, cliente, alertas):
    o = operador()
    c = cliente()
    _post(o.headers, c.conta_id, "piloto", ativo=True, motivo="liga")
    alertas.enviadas.clear()

    _post(o.headers, c.conta_id, "piloto", ativo=False, motivo="desliga")

    assert {"Piloto desligado" in m.texto for m in _alertas_enviados(alertas)} == {True}


# --- ação recusada não alerta ----------------------------------------------------------


def test_acao_recusada_nao_manda_alerta(db_session, operador, cliente, stripe_falso, alertas):
    o = operador()
    c = cliente()

    inexistente = _post(o.headers, uuid.uuid4(), "inativar", motivo="x")
    sem_motivo = _post(o.headers, c.conta_id, "inativar", motivo="   ")
    reativar_ativa = _post(o.headers, c.conta_id, "reativar", motivo="x")
    sem_assinatura = _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="x")
    nao_operador = _post(c.headers["admin"], c.conta_id, "inativar", motivo="x")

    assert [
        r.status_code
        for r in (inexistente, sem_motivo, reativar_ativa, sem_assinatura, nao_operador)
    ] == [404, 422, 409, 409, 403]
    assert alertas.enviadas == []


def test_repetir_a_acao_manda_so_o_primeiro_alerta(db_session, operador, cliente, alertas):
    o = operador()
    c = cliente()

    primeira = _inativar(o, c)
    repetida = _inativar(o, c)

    assert (primeira.status_code, repetida.status_code) == (200, 409)
    assert len(_alertas_enviados(alertas)) == len(EQUIPE)


def test_stripe_fora_do_ar_e_cobranca_desligada_nao_mandam_alerta(
    db_session, operador, cliente, stripe_falso, alertas, monkeypatch
):
    o = operador()
    c = cliente()
    _assinar(db_session, stripe_falso, c.conta_id)
    stripe_falso.erro = stripe.APIConnectionError("sem rede")

    fora_do_ar = _post(o.headers, c.conta_id, "assinatura/cancelar", quando="agora", motivo="x")
    monkeypatch.setattr(settings, "stripe_habilitado", False)
    desligada = _post(o.headers, c.conta_id, "assinatura/manter", motivo="x")

    assert (fora_do_ar.status_code, desligada.status_code) == (502, 503)
    assert alertas.enviadas == []


# --- falha, desligado ------------------------------------------------------------------


def test_falha_do_provedor_nao_muda_a_acao(db_session, operador, cliente, alertas, caplog):
    alertas._falha = RuntimeError("Resend fora do ar para equipe@ledgr.teste")
    o = operador()
    c = cliente("Escritório Alvo Ltda")

    with caplog.at_level(logging.DEBUG):
        resposta = _inativar(o, c)

    assert resposta.status_code == 200
    assert resposta.json()["inativa"] is True
    db_session.expire_all()
    assert db_session.get(Conta, c.conta_id).inativada_em is not None
    do_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert f"admin_alerta conta_id={c.conta_id} acao=inativada resultado=falha_envio" in do_app
    assert "classe=RuntimeError" in do_app
    assert "equipe@ledgr.teste" not in do_app and "Resend fora do ar" not in do_app


def test_falha_ao_montar_o_alerta_nao_muda_a_acao(
    db_session, operador, cliente, alertas, monkeypatch, caplog
):
    def quebrar(**_):
        raise ValueError("dado pessoal: Escritório Alvo")

    monkeypatch.setattr(alerta_admin, "mensagem_alerta_admin", quebrar)
    o = operador()
    c = cliente()

    with caplog.at_level(logging.DEBUG):
        resposta = _inativar(o, c)

    assert resposta.status_code == 200
    do_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert f"admin_alerta resultado=falha_preparo conta_id={c.conta_id}" in do_app
    assert "classe=ValueError" in do_app and "Escritório Alvo" not in do_app
    assert alertas.enviadas == []


@pytest.mark.parametrize("lista", ["", "   ", " , ,", "sem-arroba, outra coisa"])
def test_sem_destinatarios_nao_envia_e_so_loga_desligado(
    db_session, operador, cliente, alertas, monkeypatch, caplog, lista
):
    monkeypatch.setattr(settings, "alertas_admin_emails", lista)
    o = operador()
    c = cliente()

    with caplog.at_level(logging.DEBUG):
        resposta = _inativar(o, c)

    assert resposta.status_code == 200
    assert alertas.enviadas == []
    do_app = [r.getMessage() for r in caplog.records if r.name.startswith("app")]
    assert "admin_alerta resultado=desligado" in do_app


def test_com_o_email_desligado_nao_envia_e_so_loga_desligado(
    db_session, operador, cliente, monkeypatch, caplog
):
    # Sem a fixture `provedor`: EMAIL_HABILITADO desligado, como na suíte toda.
    monkeypatch.setattr(settings, "alertas_admin_emails", LISTA)
    assert settings.email_habilitado is False
    o = operador()
    c = cliente()

    with caplog.at_level(logging.DEBUG):
        resposta = _inativar(o, c)

    assert resposta.status_code == 200
    do_app = [r.getMessage() for r in caplog.records if r.name.startswith("app")]
    assert "admin_alerta resultado=desligado" in do_app


# --- logs ---------------------------------------------------------------------------


def test_o_log_nao_tem_email_nome_nem_motivo(db_session, operador, cliente, alertas, caplog):
    o = operador("Olga Operadora")
    c = cliente("Escritório Alvo Ltda", email="admin@alvo.com.br")

    with caplog.at_level(logging.DEBUG):
        resposta = _post(o.headers, c.conta_id, "inativar", motivo="Motivo secreto do operador")

    assert resposta.status_code == 200
    do_app = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert f"admin_alerta conta_id={c.conta_id} acao=inativada resultado=enviado" in do_app
    for proibido in (
        o.usuario.email,
        "Olga",
        "Escritório Alvo",
        "admin@alvo.com.br",
        "Motivo secreto",
        "ledgr.teste",
    ):
        assert proibido not in do_app, proibido


# --- a mensagem ---------------------------------------------------------------------


def test_a_mensagem_escapa_todo_texto_de_pessoa():
    m = mensagem_alerta_admin(
        para="equipe@ledgr.teste",
        operador_nome="Olga <b>Op</b>",
        operador_email="olga@ledgr.teste",
        conta_id="1",
        razao_social="A & B <Ltda>",
        acao="Cupom dado à conta: <i>Promo</i>",
        quando="19/11/2026 às 10:00:00",
        motivo='<img src=x onerror="alert(1)">',
    )

    for cru in ("<b>Op</b>", "<Ltda>", "<i>Promo</i>", "<img src=x"):
        assert cru not in m.html
    for escapado in ("Olga &lt;b&gt;Op&lt;/b&gt;", "A &amp; B &lt;Ltda&gt;", "&lt;img src=x"):
        assert escapado in m.html
    assert "<img" in m.texto  # o texto puro não é HTML


def test_o_assunto_sai_numa_linha_so():
    m = mensagem_alerta_admin(
        para="equipe@ledgr.teste",
        operador_nome="Olga",
        operador_email="olga@ledgr.teste",
        conta_id="1",
        razao_social="Alvo\r\nBcc: fora@exemplo.com",
        acao="Conta inativada",
        quando="agora",
        motivo="x",
    )

    assert "\n" not in m.assunto and "\r" not in m.assunto


# --- a configuração -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("", []),
        ("a@x.com", ["a@x.com"]),
        (" a@x.com , b@x.com ", ["a@x.com", "b@x.com"]),
        ("a@x.com,A@X.com,b@x.com", ["a@x.com", "b@x.com"]),
        ("a@x.com,sem-arroba,,c d@x.com", ["a@x.com"]),
    ],
)
def test_destinatarios_do_alerta(valor, esperado):
    config = Settings(_env_file=None, database_url="postgresql://x/y", alertas_admin_emails=valor)

    assert config.alertas_admin_destinatarios() == esperado


def test_alertas_vazio_e_o_padrao(monkeypatch):
    monkeypatch.delenv("ALERTAS_ADMIN_EMAILS", raising=False)
    assert (
        Settings(_env_file=None, database_url="postgresql://x/y").alertas_admin_destinatarios()
        == []
    )
