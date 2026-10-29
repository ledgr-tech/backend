"""`DELETE /me` (issue #68, ADR-017): a pessoa desativa a própria conta.

A desativação é a remoção da #69 (app/services/usuarios.py), então aqui só o
que é da rota: a confirmação (senha ou login recente do Google), a regra do
administrador, os convites, o aviso por e-mail e o que acontece depois.

Nenhum teste chama o Google nem o Resend: a fixture `google` troca o cliente
de chaves por um falso, e o aviso vai para o provedor falso.
"""

import logging
import time
import uuid

import pytest
from conftest import SENHA_USUARIO_TESTE as SENHA
from conftest import apagar_contas_por_cnpj, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.me import LIMITE_DESATIVACAO_CONTA_POR_USUARIO
from app.core.auth import LOGIN_RECENTE_SEGUNDOS
from app.models import Conta, Convite, DecisaoLinha, Empresa, Usuario
from app.models.usuario import DOMINIO_EMAIL_REMOVIDO
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)

LIMITE = int(LIMITE_DESATIVACAO_CONTA_POR_USUARIO.split("/")[0])
CONVITE_INVALIDO = {"detail": "Convite inválido ou expirado. Peça um novo convite."}


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario, auth_time=None) -> dict[str, str]:
        extra = {} if auth_time is None else {"auth_time": auth_time}
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id, **extra)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _desativar(cabecalho, corpo: dict | None = None):
    return client.request("DELETE", "/me", json=corpo, headers=cabecalho)


def _recarregar(db_session, usuario: Usuario) -> Usuario:
    db_session.expire_all()
    return db_session.get(Usuario, usuario.id)


def _login(email: str, senha: str = SENHA):
    return client.post("/login", json={"email": email, "senha": senha})


@pytest.fixture
def limpar_cadastro(db_session):
    """Apaga a conta, a empresa e o usuário que um `/register` do teste criou."""
    cnpjs: list[str] = []
    yield cnpjs.append
    db_session.rollback()
    apagar_contas_por_cnpj(db_session, cnpjs)
    db_session.commit()


# --- conta com senha ---------------------------------------------------------


def test_senha_certa_desativa_e_tudo_o_que_vem_depois(
    db_session, criar_usuario, cabecalho_de, gerar_cnpj, limpar_cadastro
):
    usuario = criar_usuario(nome="Maria Financeiro")
    email = usuario.email
    cabecalho = cabecalho_de(usuario)

    response = _desativar(cabecalho, {"senha_atual": SENHA})

    assert response.status_code == 204
    assert response.content == b""
    desativado = _recarregar(db_session, usuario)
    assert desativado.removido_em is not None
    assert desativado.nome == "Maria Financeiro"
    assert desativado.email == f"removido+{usuario.id}@{DOMINIO_EMAIL_REMOVIDO}"
    assert desativado.senha_hash is None
    assert desativado.sessoes_validas_desde is not None

    # O token de antes não vale mais, nem para a própria rota.
    assert client.get("/me", headers=cabecalho).status_code == 401
    assert _desativar(cabecalho, {"senha_atual": SENHA}).status_code == 401
    # O login responde como senha errada.
    assert _login(email).status_code == 401

    # E o e-mail fica livre para um cadastro novo.
    cnpj = gerar_cnpj()
    limpar_cadastro(cnpj)
    cadastro = client.post(
        "/register",
        json={
            "nome": "Maria de Novo",
            "email": email,
            "senha": "senha-forte-123",
            "razao_social": "Empresa Nova",
            "cnpj": cnpj,
        },
    )
    assert cadastro.status_code == 201
    assert cadastro.json()["id"] != str(usuario.id)


@pytest.mark.parametrize(
    "corpo",
    [{"senha_atual": "senha-errada-999"}, {"senha_atual": ""}, {}, None],
    ids=["errada", "vazia", "sem_campo", "sem_corpo"],
)
def test_senha_errada_ou_ausente_e_400_e_nada_muda(db_session, criar_usuario, cabecalho_de, corpo):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)

    response = _desativar(cabecalho, corpo)

    assert response.status_code == 400
    assert response.json() == {"detail": "Senha atual incorreta."}
    assert _recarregar(db_session, usuario).removido_em is None
    assert client.get("/me", headers=cabecalho).status_code == 200


# --- conta só do Google ------------------------------------------------------


def test_conta_google_com_login_recente_desativa(db_session, google, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)
    sub, email = usuario.google_sub, usuario.email

    # A senha mandada pelo front, se vier, não importa: não há o que conferir.
    response = _desativar(cabecalho_de(usuario), {"senha_atual": ""})

    assert response.status_code == 204
    desativado = _recarregar(db_session, usuario)
    assert desativado.removido_em is not None
    assert desativado.google_sub is None
    # O login pelo Google não acha mais a conta, nem pelo `sub`, nem pelo e-mail.
    login = client.post("/login/google", json={"id_token": google.id_token(email=email, sub=sub)})
    assert login.status_code == 404


def test_conta_google_com_login_de_quatro_minutos_ainda_vale(
    db_session, criar_usuario, cabecalho_de
):
    usuario = criar_usuario(com_senha=False)
    auth_time = int(time.time()) - (LOGIN_RECENTE_SEGUNDOS - 60)

    assert _desativar(cabecalho_de(usuario, auth_time=auth_time)).status_code == 204


def test_conta_google_com_login_antigo_e_409_e_nada_muda(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)
    cabecalho = cabecalho_de(usuario, auth_time=int(time.time()) - LOGIN_RECENTE_SEGUNDOS - 60)

    response = _desativar(cabecalho)

    assert response.status_code == 409
    assert response.json() == {"detail": "Entre de novo com o Google para confirmar a desativação."}
    assert _recarregar(db_session, usuario).removido_em is None
    assert client.get("/me", headers=cabecalho).status_code == 200


def test_login_recente_e_de_cinco_minutos():
    assert LOGIN_RECENTE_SEGUNDOS == 5 * 60


# --- administrador -----------------------------------------------------------


def test_administrador_com_outro_usuario_ativo_e_409(db_session, criar_usuario, cabecalho_de):
    admin = criar_usuario()
    convidado = criar_usuario(conta_id=admin.conta_id)

    response = _desativar(cabecalho_de(admin), {"senha_atual": SENHA})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Transfira o papel de admin antes de desativar a sua conta."
    }
    assert _recarregar(db_session, admin).removido_em is None
    assert _recarregar(db_session, convidado).removido_em is None


def test_administrador_com_convite_pendente_e_outro_usuario_nao_perde_o_convite(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin = criar_usuario()
    criar_usuario(conta_id=admin.conta_id)
    assert (
        client.post(
            "/empresa/convites", json={"email": "novo@teste.com"}, headers=cabecalho_de(admin)
        ).status_code
        == 201
    )

    assert _desativar(cabecalho_de(admin), {"senha_atual": SENHA}).status_code == 409

    db_session.expire_all()
    convite = db_session.scalars(select(Convite).where(Convite.conta_id == admin.conta_id)).one()
    assert convite.cancelado_em is None


def test_administrador_sozinho_desativa_e_a_empresa_fica(db_session, criar_usuario, cabecalho_de):
    admin = criar_usuario()
    remover_usuario(db_session, criar_usuario(conta_id=admin.conta_id))
    db_session.commit()

    response = _desativar(cabecalho_de(admin), {"senha_atual": SENHA})

    assert response.status_code == 204
    assert _recarregar(db_session, admin).removido_em is not None
    # Os dados da conta só saem pelo script (scripts/excluir_empresa.py).
    assert db_session.get(Conta, admin.conta_id) is not None
    assert db_session.get(Empresa, empresa_unica(db_session, admin.conta_id)) is not None


def test_desativacao_cancela_os_convites_pendentes_de_quem_saiu(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin = criar_usuario()
    resposta = client.post(
        "/empresa/convites", json={"email": "novo@teste.com"}, headers=cabecalho_de(admin)
    )
    assert resposta.status_code == 201
    link = provedor.enviadas[-1].texto
    token = link.split("#token=", 1)[1].split()[0]

    assert _desativar(cabecalho_de(admin), {"senha_atual": SENHA}).status_code == 204

    db_session.expire_all()
    convite = db_session.get(Convite, uuid.UUID(resposta.json()["id"]))
    assert convite.cancelado_em is not None
    aceite = client.post(
        "/convites/aceitar", json={"token": token, "nome": "Novo", "senha": "senha-forte-123"}
    )
    assert aceite.status_code == 400
    assert aceite.json() == CONVITE_INVALIDO


# --- convidado ---------------------------------------------------------------


def test_convidado_desativa_e_as_decisoes_continuam_com_o_nome(
    db_session, criar_usuario, cabecalho_de, criar_extrato_da_empresa
):
    admin = criar_usuario()
    convidado = criar_usuario(conta_id=admin.conta_id, nome="Bruno Convidado")
    empresa_id = empresa_unica(db_session, admin.conta_id)
    banco = criar_extrato_da_empresa(empresa_id, "banco")
    sistema = criar_extrato_da_empresa(empresa_id, "sistema")
    decisao = DecisaoLinha(
        empresa_id=empresa_id,
        extrato_banco_id=banco.id,
        chave="c" * 64,
        tipo="conferida",
        usuario_id=convidado.id,
        autor_nome=convidado.nome,
        rodada=1,
        extrato_sistema_id=sistema.id,
    )
    db_session.add(decisao)
    db_session.commit()

    response = _desativar(cabecalho_de(convidado), {"senha_atual": SENHA})

    assert response.status_code == 204
    db_session.expire_all()
    decisao = db_session.get(DecisaoLinha, decisao.id)
    assert decisao.usuario_id == convidado.id
    assert decisao.autor_nome == "Bruno Convidado"
    assert db_session.get(Usuario, convidado.id).nome == "Bruno Convidado"
    # O administrador continua, e a equipe não lista mais o convidado.
    equipe = client.get("/empresa/usuarios", headers=cabecalho_de(admin))
    assert [u["id"] for u in equipe.json()["itens"]] == [str(admin.id)]


# --- aviso por e-mail --------------------------------------------------------


def test_aviso_vai_para_o_email_antigo_depois_da_desativacao(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin = criar_usuario(nome="Ana Administradora")
    convidado = criar_usuario(conta_id=admin.conta_id, nome="Bruno Convidado")
    email = convidado.email

    assert _desativar(cabecalho_de(convidado), {"senha_atual": SENHA}).status_code == 204

    [aviso] = provedor.enviadas
    assert aviso.para == email
    assert aviso.assunto == "Sua conta da Ledgr foi desativada"
    assert "Bruno Convidado" in aviso.texto
    # O convidado não deixa a empresa sem acesso: nada sobre os dados dela.
    assert "Empresa de teste" not in aviso.texto


def test_aviso_do_administrador_sozinho_fala_dos_dados_da_empresa(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin = criar_usuario()

    assert _desativar(cabecalho_de(admin), {"senha_atual": SENHA}).status_code == 204

    [aviso] = provedor.enviadas
    assert "Os dados da empresa Empresa de teste continuam guardados" in aviso.texto
    assert "Empresa de teste" in aviso.html


def test_com_email_desligado_desativa_sem_aviso(db_session, criar_usuario, cabecalho_de, caplog):
    caplog.set_level(logging.INFO)
    usuario = criar_usuario()

    assert _desativar(cabecalho_de(usuario), {"senha_atual": SENHA}).status_code == 204
    assert "aviso_conta_desativada resultado=desabilitado" in caplog.text


# --- limites, autenticação e logs -------------------------------------------


def test_sem_token_e_401():
    assert _desativar({}, {"senha_atual": SENHA}).status_code == 401


def test_rate_limit_por_usuario(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    outro = criar_usuario()
    cabecalho = cabecalho_de(usuario)

    for _ in range(LIMITE):
        assert _desativar(cabecalho, {"senha_atual": "errada"}).status_code == 400

    assert _desativar(cabecalho, {"senha_atual": SENHA}).status_code == 429
    assert _recarregar(db_session, usuario).removido_em is None
    # O limite é por usuário, não por IP.
    assert _desativar(cabecalho_de(outro), {"senha_atual": "errada"}).status_code == 400


def test_logs_nao_tem_email_nem_nome(db_session, provedor, criar_usuario, cabecalho_de, caplog):
    caplog.set_level(logging.DEBUG)
    admin = criar_usuario(nome="Ana Administradora")
    convidado = criar_usuario(conta_id=admin.conta_id, nome="Bruno Convidado")
    google = criar_usuario(com_senha=False, conta_id=admin.conta_id, nome="Carla Google")
    emails = [admin.email, convidado.email, google.email]

    _desativar(cabecalho_de(convidado), {"senha_atual": "errada"})
    _desativar(cabecalho_de(admin), {"senha_atual": SENHA})
    _desativar(cabecalho_de(google, auth_time=int(time.time()) - 3600))
    _desativar(cabecalho_de(convidado), {"senha_atual": SENHA})

    for resultado in [
        "senha_incorreta",
        "administrador_com_usuarios",
        "login_antigo",
        "desativada",
    ]:
        assert f"desativacao_conta resultado={resultado}" in caplog.text
    assert "aviso_conta_desativada resultado=enviado" in caplog.text
    for texto in [*emails, "Ana", "Bruno", "Carla", "@teste.com"]:
        assert texto not in caplog.text
