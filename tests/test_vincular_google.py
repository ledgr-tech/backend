"""Vínculo mediante senha atual (issue #218), com PostgreSQL e tokens RSA reais."""

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import SENHA_USUARIO_TESTE, apagar_contas_por_cnpj
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.core import bloqueio_login, senha
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models import Usuario
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)
ROTA = "/login/google/vincular"


def _vincular(token, senha_recebida=SENHA_USUARIO_TESTE, headers=None):
    return client.post(ROTA, json={"id_token": token, "senha": senha_recebida}, headers=headers)


@pytest.fixture
def cadastrado_com_senha(db_session, gerar_cnpj):
    cnpj = gerar_cnpj()
    try:
        resposta = client.post(
            "/register",
            json={
                "nome": "Maria Financeiro",
                "email": f"aceite-{cnpj}@teste.com",
                "senha": SENHA_USUARIO_TESTE,
                "cnpj": cnpj,
                "razao_social": "Empresa do aceite",
            },
        )
        assert resposta.status_code == 201
        yield db_session.scalar(select(Usuario).where(Usuario.email == resposta.json()["email"]))
    finally:
        db_session.rollback()
        apagar_contas_por_cnpj(db_session, [cnpj])
        db_session.commit()


def test_aceite_em_sequencia(db_session, cadastrado_com_senha, google, gerar_token, provedor):
    usuario = cadastrado_com_senha
    # O cadastro já pode ter enviado as boas-vindas.
    provedor.enviadas.clear()
    hash_antes = usuario.senha_hash
    token = google.id_token(email=usuario.email.upper(), sub="google-aceite-218")
    assert client.post("/login/google", json={"id_token": token}).status_code == 409
    db_session.refresh(usuario)
    assert usuario.google_sub is None
    antes = time.time()

    resposta = _vincular(token)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo == {
        "id": str(usuario.id),
        "conta_id": str(usuario.conta_id),
        "empresa_id": str(usuario.conta_id),
        "nome": usuario.nome,
        "email": usuario.email,
        "auth_time": corpo["auth_time"],
    }
    assert type(corpo["auth_time"]) is int
    assert math.floor(antes) <= corpo["auth_time"] <= time.time()
    db_session.refresh(usuario)
    assert usuario.google_sub == "google-aceite-218"
    assert usuario.senha_hash == hash_antes
    assert usuario.ultimo_login_em is not None
    assert client.post("/login/google", json={"id_token": token}).status_code == 200
    assert (
        client.post(
            "/login", json={"email": usuario.email, "senha": SENHA_USUARIO_TESTE}
        ).status_code
        == 200
    )
    jwt = gerar_token(usuario.conta_id, usuario_id=usuario.id, auth_time=corpo["auth_time"])
    me = client.get("/me", headers={"Authorization": f"Bearer {jwt}"})
    assert me.status_code == 200
    assert me.json()["metodos_login"] == ["senha", "google"]
    assert len(provedor.enviadas) == 1


def test_401_identico_sem_gravacao(db_session, criar_usuario, google, provedor):
    usuario = criar_usuario()
    so_google = criar_usuario(com_senha=False)
    resposta_login = client.post("/login", json={"email": usuario.email, "senha": "errada"})
    for email in (usuario.email, so_google.email, "nao-existe-218@example.com"):
        resposta = _vincular(google.id_token(email=email), "errada")
        assert resposta.status_code == resposta_login.status_code == 401
        assert resposta.json() == resposta_login.json() == {"detail": "E-mail ou senha inválidos."}
    db_session.refresh(usuario)
    assert usuario.google_sub is None
    assert usuario.ultimo_login_em is None
    assert provedor.enviadas == []


def test_usuario_desativado_como_login(db_session, criar_usuario, google, provedor):
    usuario = criar_usuario()
    email = usuario.email
    remover_usuario(db_session, usuario)
    db_session.commit()
    for endereco in (email, usuario.email):
        login = client.post("/login", json={"email": endereco, "senha": SENHA_USUARIO_TESTE})
        vinculo = _vincular(google.id_token(email=endereco))
        assert vinculo.status_code == login.status_code == 401
        assert vinculo.json() == login.json()
    db_session.refresh(usuario)
    assert usuario.google_sub is None
    assert provedor.enviadas == []


@pytest.mark.parametrize("outro_usuario", [False, True])
def test_conflitos_preservam_vinculos(db_session, criar_usuario, google, provedor, outro_usuario):
    usuario = criar_usuario()
    dono = criar_usuario(com_senha=False) if outro_usuario else usuario
    if not outro_usuario:
        dono.google_sub = "google-anterior-218"
        db_session.commit()
    sub_antes = dono.google_sub
    sub = sub_antes if outro_usuario else "google-diferente-218"

    resposta = _vincular(google.id_token(email=usuario.email, sub=sub))

    assert resposta.status_code == 409
    db_session.refresh(dono)
    db_session.refresh(usuario)
    assert dono.google_sub == sub_antes
    assert usuario.google_sub == (None if outro_usuario else sub_antes)
    assert usuario.ultimo_login_em is None
    assert provedor.enviadas == []


def test_retentativa_exige_senha_novamente_sem_repetir_aviso(
    db_session, criar_usuario, google, provedor
):
    usuario = criar_usuario()
    token = google.id_token(email=usuario.email)
    assert _vincular(token).status_code == 200
    assert _vincular(token, "errada").status_code == 401
    # A senha mudou desde o vínculo: só a nova é aceita.
    db_session.refresh(usuario)
    usuario.senha_hash = senha.gerar_hash("senha-nova-218")
    db_session.commit()
    assert _vincular(token).status_code == 401
    assert _vincular(token, "senha-nova-218").status_code == 200
    assert len(provedor.enviadas) == 1


def test_bloqueio_por_email_compartilhado_com_login(db_session, criar_usuario, google):
    usuario = criar_usuario()
    token = google.id_token(email=usuario.email)
    for _ in range(bloqueio_login.MAXIMO_FALHAS - 1):
        assert _vincular(token, "errada").status_code == 401
    assert (
        client.post("/login", json={"email": usuario.email, "senha": "errada"}).status_code == 401
    )
    resposta = _vincular(token)
    assert resposta.status_code == 429
    assert int(resposta.headers["Retry-After"]) > 0
    db_session.refresh(usuario)
    assert usuario.google_sub is None


def test_aviso_so_depois_do_commit(db_session, criar_usuario, google, provedor, monkeypatch):
    usuario = criar_usuario()
    usuario_id, email = usuario.id, usuario.email
    token = google.id_token(email=email, sub="google-aviso-218")
    enviar = provedor.enviar

    def conferir_commit(mensagem):
        with SessionLocal() as db:
            assert db.get(Usuario, usuario_id).google_sub == "google-aviso-218"
        enviar(mensagem)

    monkeypatch.setattr(provedor, "enviar", conferir_commit)
    assert _vincular(token).status_code == 200
    (aviso,) = provedor.enviadas
    assert aviso.para == email
    assert aviso.assunto == "O Google foi conectado à sua conta do Ledgr"
    assert "O Google foi conectado à sua conta do Ledgr" in aviso.texto
    assert aviso.chave_idempotencia == f"google-conectado-{usuario_id}"


def test_email_desligado_vincula_sem_aviso(db_session, criar_usuario, google, caplog):
    usuario = criar_usuario()
    caplog.set_level(logging.INFO)
    assert _vincular(google.id_token(email=usuario.email)).status_code == 200
    assert "aviso_google_conectado resultado=desabilitado" in caplog.text


def test_falha_de_email_nao_desfaz_vinculo_nem_repete_envio(
    db_session, criar_usuario, google, provedor, monkeypatch, caplog
):
    usuario = criar_usuario()
    token = google.id_token(email=usuario.email, sub="google-falha-email-218")
    tentativas = []

    def falhar(mensagem):
        tentativas.append(mensagem)
        raise RuntimeError("falha com dado sensível que não deve ir a log")

    monkeypatch.setattr(provedor, "enviar", falhar)
    caplog.set_level(logging.INFO)
    assert _vincular(token).status_code == 200
    assert _vincular(token).status_code == 200
    db_session.refresh(usuario)
    assert usuario.google_sub == "google-falha-email-218"
    assert len(tentativas) == 1
    assert "aviso_google_conectado resultado=falha_envio classe=RuntimeError" in caplog.text
    assert "dado sensível" not in caplog.text


@pytest.mark.parametrize("segredo", ["segredo-proxy-218", "errado", None])
def test_limite_por_ip_e_confianca_no_proxy(db_session, google, monkeypatch, segredo):
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", "segredo-proxy-218")
    # Tokens inválidos isolam o contador por IP do bloqueio por e-mail.
    headers = {"X-Forwarded-For": "76.76.21.21", "X-Ledgr-IP-Cliente": "198.51.100.7"}
    if segredo is not None:
        headers["X-Ledgr-Segredo-Proxy"] = segredo
    assert [_vincular("invalido", headers=headers).status_code for _ in range(11)] == [
        *([401] * 10),
        429,
    ]
    headers["X-Ledgr-IP-Cliente"] = "198.51.100.8"
    esperado = 401 if segredo == "segredo-proxy-218" else 429
    assert _vincular("invalido", headers=headers).status_code == esperado


@pytest.mark.parametrize(
    "campo,valor", [("id_token", None), ("id_token", ""), ("senha", None), ("senha", "")]
)
def test_campos_obrigatorios(db_session, campo, valor):
    corpo = {"id_token": "token", "senha": SENHA_USUARIO_TESTE}
    if valor is None:
        corpo.pop(campo)
    else:
        corpo[campo] = valor
    assert client.post(ROTA, json=corpo).status_code == 422


@pytest.mark.parametrize("corrida", ["mesmo_vinculo", "outro_sub", "outro_usuario"])
def test_vinculos_concorrentes(db_session, criar_usuario, google, provedor, corrida, caplog):
    primeiro = criar_usuario()
    segundo = criar_usuario() if corrida == "outro_usuario" else primeiro
    ids = [primeiro.id, segundo.id]
    subs = [
        "google-corrida-218",
        "google-outro-218" if corrida == "outro_sub" else "google-corrida-218",
    ]
    tokens = [google.id_token(email=u.email, sub=s) for u, s in zip((primeiro, segundo), subs)]
    barreira = Barrier(2, timeout=10)

    def sincronizar(conn, cursor, statement, parameters, context, executemany):
        # Mesma conta: as duas requisições chegam juntas ao FOR UPDATE.
        # Contas distintas: ambas passaram pela consulta de sub antes de gravar,
        # obrigando a UNIQUE do PostgreSQL a resolver a disputa no commit.
        if corrida == "outro_usuario":
            if statement.startswith("UPDATE usuarios SET") and "google_sub=" in statement:
                barreira.wait()
        elif statement.rstrip().endswith("FOR UPDATE"):
            barreira.wait()

    db_session.rollback()  # nenhum lock do teste durante as requisições
    caplog.set_level(logging.INFO)
    event.listen(engine, "before_cursor_execute", sincronizar)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            respostas = list(executor.map(_vincular, tokens))
    finally:
        event.remove(engine, "before_cursor_execute", sincronizar)
    esperado = [200, 200] if corrida == "mesmo_vinculo" else [200, 409]
    assert sorted(r.status_code for r in respostas) == esperado
    vencedor = next(i for i, r in enumerate(respostas) if r.status_code == 200)
    db_session.expire_all()
    assert db_session.get(Usuario, ids[vencedor]).google_sub == subs[vencedor]
    vinculados = db_session.scalars(select(Usuario).where(Usuario.id.in_(ids))).all()
    assert sum(u.google_sub is not None for u in vinculados) == 1
    assert len(provedor.enviadas) == 1
    if corrida == "outro_usuario":
        assert "vinculo_google resultado=conflito_concorrente" in caplog.text
    for segredo in (*tokens, *subs, primeiro.email, segundo.email, SENHA_USUARIO_TESTE):
        assert segredo not in caplog.text


def test_logs_sem_credenciais(db_session, criar_usuario, google, provedor, caplog):
    usuario = criar_usuario()
    sub = "google-segredo-logs-218"
    token = google.id_token(email=usuario.email, sub=sub)
    invalido = google.id_token(email=usuario.email, email_verified=False)
    caplog.set_level(logging.DEBUG)
    assert _vincular(invalido).status_code == 401
    assert _vincular(token, "senha-errada-secreta-218").status_code == 401
    assert _vincular(token).status_code == 200
    assert _vincular(token).status_code == 200
    assert (
        _vincular(google.id_token(email=usuario.email, sub="outro-sub-secreto")).status_code == 409
    )
    assert "vinculo_google resultado=conectado" in caplog.text
    for segredo in (
        token,
        invalido,
        SENHA_USUARIO_TESTE,
        "senha-errada-secreta-218",
        usuario.email,
        sub,
        "outro-sub-secreto",
    ):
        assert segredo not in caplog.text
