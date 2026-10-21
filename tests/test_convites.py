"""Testes do convite de usuário (issue #69, ADR-015): `POST`, `GET` e `DELETE`
/empresa/convites, do administrador, e `POST /convites/consultar` e
`/convites/aceitar`, públicas.

O provedor de e-mail é o falso do conftest (fixture `provedor`): nenhum teste
chama o Resend. O `TestClient` roda as `BackgroundTasks` antes de devolver a
resposta, então o e-mail já saiu quando o teste confere.
"""

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FRONTEND_URL_TESTE as FRONTEND_URL
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api.equipe import LIMITE_CONVITES_POR_HORA
from app.core.config import settings
from app.models import Convite, Usuario
from app.services.email import MensagemEmail
from main import app

client = TestClient(app)

SENHA_CONVIDADO = "senha-do-convidado-1"
CONVITE_INVALIDO = {"detail": "Convite inválido ou expirado. Peça um novo convite."}


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


@pytest.fixture
def admin(db_session, criar_usuario):
    return criar_usuario(nome="Ana Administradora")


def _email_novo() -> str:
    return f"{uuid.uuid4().hex[:12]}@convidado.com"


def _convidar(cabecalho, email: str):
    return client.post("/empresa/convites", json={"email": email}, headers=cabecalho)


def _consultar(token: str):
    return client.post("/convites/consultar", json={"token": token})


def _aceitar(token: str, nome: str = "Joana Convidada", senha: str = SENHA_CONVIDADO):
    return client.post("/convites/aceitar", json={"token": token, "nome": nome, "senha": senha})


def _token_do_link(mensagem: MensagemEmail) -> str:
    link = re.search(r"https://\S+", mensagem.texto).group(0)
    assert link.startswith(f"{FRONTEND_URL}/aceitar-convite#token=")
    return link.split("#token=", 1)[1]


def _convidar_e_pegar_token(cabecalho, provedor, email: str) -> str:
    response = _convidar(cabecalho, email)
    assert response.status_code == 201, response.text
    return _token_do_link(provedor.enviadas[-1])


def _convites_da_conta(db_session, conta_id) -> list[Convite]:
    db_session.expire_all()
    return list(db_session.scalars(select(Convite).where(Convite.conta_id == conta_id)))


# --- fluxo completo ------------------------------------------------------------


def test_fluxo_completo_convidar_consultar_aceitar_e_entrar(
    db_session, provedor, admin, cabecalho_de
):
    email = _email_novo()

    response = _convidar(cabecalho_de(admin), email.upper())

    assert response.status_code == 201
    corpo = response.json()
    assert set(corpo) == {"id", "email", "criado_em", "expira_em"}
    assert corpo["email"] == email
    expira_em = datetime.fromisoformat(corpo["expira_em"])
    assert expira_em - datetime.now(UTC) > timedelta(days=6, hours=23)
    assert expira_em - datetime.now(UTC) <= timedelta(days=7)

    (mensagem,) = provedor.enviadas
    assert mensagem.para == email
    assert mensagem.chave_idempotencia == f"convite/{corpo['id']}"
    assert admin.conta.razao_social in mensagem.texto
    assert "Ana Administradora" in mensagem.texto
    token = _token_do_link(mensagem)
    assert len(token) >= 43

    consulta = _consultar(token)
    assert consulta.status_code == 200
    assert consulta.json() == {
        "email": email,
        "razao_social": admin.conta.razao_social,
        "expira_em": corpo["expira_em"],
    }

    aceite = _aceitar(token, nome="  Joana Convidada  ")
    assert aceite.status_code == 201
    novo = aceite.json()
    assert set(novo) == {"id", "conta_id", "empresa_id", "nome", "email"}
    assert novo["conta_id"] == novo["empresa_id"] == str(admin.conta_id)
    assert novo["nome"] == "Joana Convidada"
    assert novo["email"] == email

    login = client.post("/login", json={"email": email, "senha": SENHA_CONVIDADO})
    assert login.status_code == 200
    # o `auth_time` é só do login (issue #75); o aceite devolve o mesmo do /register
    assert {k: v for k, v in login.json().items() if k != "auth_time"} == novo

    usuario = db_session.get(Usuario, uuid.UUID(novo["id"]))
    # Até o convite ter papel (parte 2 da issue #133), o convidado é analista.
    assert (usuario.papel, usuario.conta_id) == ("analista", admin.conta_id)
    (convite,) = _convites_da_conta(db_session, admin.conta_id)
    assert convite.usado_em is not None
    assert convite.convidado_por_id == admin.id
    # O aceite não manda boas-vindas: só o e-mail do convite saiu.
    assert len(provedor.enviadas) == 1


def test_token_e_guardado_so_como_hash(db_session, provedor, admin, cabecalho_de):
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, _email_novo())

    (convite,) = _convites_da_conta(db_session, admin.conta_id)
    assert convite.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in [str(valor) for valor in vars(convite).values()]


def test_convidado_entra_na_lista_de_usuarios(db_session, provedor, admin, cabecalho_de):
    email = _email_novo()
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, email)
    _aceitar(token)

    response = client.get("/empresa/usuarios", headers=cabecalho_de(admin))

    emails = [u["email"] for u in response.json()["itens"]]
    assert emails == [admin.email, email]


# --- convite que não vale mais -----------------------------------------------


def test_token_inexistente_e_400(db_session):
    assert _consultar("token-que-nao-existe").json() == CONVITE_INVALIDO
    response = _aceitar("token-que-nao-existe")
    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO


def test_convite_expirado_e_400(db_session, provedor, admin, cabecalho_de):
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, _email_novo())
    db_session.execute(
        update(Convite)
        .where(Convite.conta_id == admin.conta_id)
        .values(expira_em=func.now() - timedelta(minutes=1))
    )
    db_session.commit()

    assert _consultar(token).status_code == 400
    response = _aceitar(token)
    assert response.status_code == 400
    assert response.json() == CONVITE_INVALIDO
    assert client.get("/empresa/convites", headers=cabecalho_de(admin)).json() == {"itens": []}


def test_convite_reutilizado_e_400(db_session, provedor, admin, cabecalho_de):
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, _email_novo())
    assert _aceitar(token).status_code == 201

    segunda = _aceitar(token, nome="Outra Pessoa")

    assert segunda.status_code == 400
    assert segunda.json() == CONVITE_INVALIDO
    assert _consultar(token).status_code == 400


def test_convite_cancelado_e_400(db_session, provedor, admin, cabecalho_de):
    cabecalho = cabecalho_de(admin)
    convite_id = _convidar(cabecalho, _email_novo()).json()["id"]
    token = _token_do_link(provedor.enviadas[-1])

    response = client.delete(f"/empresa/convites/{convite_id}", headers=cabecalho)

    assert response.status_code == 204
    assert _consultar(token).json() == CONVITE_INVALIDO
    assert _aceitar(token).json() == CONVITE_INVALIDO
    assert client.get("/empresa/convites", headers=cabecalho).json() == {"itens": []}
    # Cancelar de novo: já não está pendente.
    assert client.delete(f"/empresa/convites/{convite_id}", headers=cabecalho).status_code == 404


def test_convite_novo_para_o_mesmo_email_substitui_o_pendente(
    db_session, provedor, admin, cabecalho_de
):
    cabecalho = cabecalho_de(admin)
    email = _email_novo()
    token_antigo = _convidar_e_pegar_token(cabecalho, provedor, email)
    token_novo = _convidar_e_pegar_token(cabecalho, provedor, email)

    assert _consultar(token_antigo).json() == CONVITE_INVALIDO
    assert _consultar(token_novo).status_code == 200
    pendentes = client.get("/empresa/convites", headers=cabecalho).json()["itens"]
    assert [c["email"] for c in pendentes] == [email]
    assert _aceitar(token_novo).status_code == 201


def test_convite_cancelado_nao_atinge_o_de_outra_empresa(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin_a, admin_b = criar_usuario(), criar_usuario()
    email = _email_novo()
    token_a = _convidar_e_pegar_token(cabecalho_de(admin_a), provedor, email)
    token_b = _convidar_e_pegar_token(cabecalho_de(admin_b), provedor, email)

    # O mesmo e-mail convidado por duas empresas: os dois valem até um ser
    # aceito; o outro esbarra no e-mail já cadastrado.
    assert _consultar(token_a).status_code == 200
    assert _aceitar(token_b).status_code == 201
    assert _aceitar(token_a).status_code == 409


# --- e-mail que já tem conta ---------------------------------------------------


def test_convidar_email_que_ja_tem_conta_e_409(
    db_session, provedor, admin, criar_usuario, cabecalho_de
):
    de_outra_empresa = criar_usuario()

    for email in (de_outra_empresa.email, admin.email, f" {admin.email.upper()} "):
        response = _convidar(cabecalho_de(admin), email.strip())
        assert response.status_code == 409
        assert response.json() == {"detail": "E-mail já cadastrado."}

    assert _convites_da_conta(db_session, admin.conta_id) == []
    assert provedor.enviadas == []


def test_aceitar_com_email_que_ganhou_conta_no_meio_tempo_e_409(
    db_session, provedor, admin, criar_usuario, cabecalho_de
):
    email = _email_novo()
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, email)
    outro = criar_usuario()
    outro.email = email  # cadastrou-se sozinho depois do convite
    db_session.commit()

    response = _aceitar(token)

    assert response.status_code == 409
    assert response.json() == {"detail": "E-mail já cadastrado."}
    (convite,) = _convites_da_conta(db_session, admin.conta_id)
    assert convite.usado_em is None


# --- validação ---------------------------------------------------------------


def test_aceitar_com_senha_curta_e_422_e_nao_consome_o_convite(
    db_session, provedor, admin, cabecalho_de
):
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, _email_novo())

    response = _aceitar(token, senha="curta")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "senha"]
    assert _consultar(token).status_code == 200


def test_aceitar_com_nome_em_branco_e_422(db_session, provedor, admin, cabecalho_de):
    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, _email_novo())

    response = _aceitar(token, nome="   ")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "nome"]


def test_convidar_email_invalido_e_422(db_session, provedor, admin, cabecalho_de):
    response = _convidar(cabecalho_de(admin), "nao-e-email")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "email"]


# --- permissão e isolamento ------------------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "caminho"),
    [
        ("post", "/empresa/convites"),
        ("get", "/empresa/convites"),
        ("delete", "/empresa/convites/{convite_id}"),
    ],
)
@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_quem_nao_e_admin_recebe_403(
    db_session, provedor, admin, criar_usuario, cabecalho_de, metodo, caminho, papel
):
    convite_id = _convidar(cabecalho_de(admin), _email_novo()).json()["id"]
    convidado = criar_usuario(conta_id=admin.conta_id, papel=papel)

    response = client.request(
        metodo,
        caminho.format(convite_id=convite_id),
        headers=cabecalho_de(convidado),
        json={"email": _email_novo()} if metodo == "post" else None,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Só o admin da conta pode fazer isso."}
    assert len(_convites_da_conta(db_session, admin.conta_id)) == 1
    assert _convites_da_conta(db_session, admin.conta_id)[0].cancelado_em is None


@pytest.mark.parametrize(
    ("metodo", "caminho"),
    [
        ("post", "/empresa/convites"),
        ("get", "/empresa/convites"),
        ("delete", f"/empresa/convites/{uuid.uuid4()}"),
    ],
)
def test_sem_token_e_401(metodo, caminho):
    assert client.request(metodo, caminho).status_code == 401


def test_lista_so_os_convites_pendentes_da_propria_empresa(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin_a, admin_b = criar_usuario(), criar_usuario()
    email_a = _email_novo()
    _convidar(cabecalho_de(admin_a), email_a)
    _convidar(cabecalho_de(admin_b), _email_novo())

    response = client.get("/empresa/convites", headers=cabecalho_de(admin_a))

    assert response.status_code == 200
    (item,) = response.json()["itens"]
    assert item["email"] == email_a
    assert set(item) == {"id", "email", "criado_em", "expira_em"}


def test_cancelar_convite_de_outra_empresa_e_404_como_inexistente(
    db_session, provedor, criar_usuario, cabecalho_de
):
    admin_a, admin_b = criar_usuario(), criar_usuario()
    convite_b = _convidar(cabecalho_de(admin_b), _email_novo()).json()["id"]

    de_outra = client.delete(f"/empresa/convites/{convite_b}", headers=cabecalho_de(admin_a))
    inexistente = client.delete(f"/empresa/convites/{uuid.uuid4()}", headers=cabecalho_de(admin_a))

    assert de_outra.status_code == inexistente.status_code == 404
    assert de_outra.json() == inexistente.json() == {"detail": "Convite não encontrado."}
    (convite,) = _convites_da_conta(db_session, admin_b.conta_id)
    assert convite.cancelado_em is None


# --- limite por hora -----------------------------------------------------------


def test_limite_de_convites_por_hora_por_empresa(db_session, provedor, criar_usuario, cabecalho_de):
    admin_a, admin_b = criar_usuario(), criar_usuario()
    for _ in range(LIMITE_CONVITES_POR_HORA):
        assert _convidar(cabecalho_de(admin_a), _email_novo()).status_code == 201

    response = _convidar(cabecalho_de(admin_a), _email_novo())

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 3600
    assert len(_convites_da_conta(db_session, admin_a.conta_id)) == LIMITE_CONVITES_POR_HORA
    assert len(provedor.enviadas) == LIMITE_CONVITES_POR_HORA
    # O limite é da empresa: a outra continua convidando.
    assert _convidar(cabecalho_de(admin_b), _email_novo()).status_code == 201


def test_convite_de_mais_de_uma_hora_atras_nao_conta_no_limite(
    db_session, provedor, admin, cabecalho_de
):
    for _ in range(LIMITE_CONVITES_POR_HORA):
        _convidar(cabecalho_de(admin), _email_novo())
    db_session.execute(
        update(Convite)
        .where(Convite.conta_id == admin.conta_id)
        .values(criado_em=func.now() - timedelta(hours=1, minutes=1))
    )
    db_session.commit()

    assert _convidar(cabecalho_de(admin), _email_novo()).status_code == 201


# --- e-mail indisponível -------------------------------------------------------


def test_email_desligado_e_503_sem_gravar_convite(db_session, admin, cabecalho_de, monkeypatch):
    # Sem a fixture `provedor`: o conftest deixa EMAIL_HABILITADO desligado.
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL)

    response = _convidar(cabecalho_de(admin), _email_novo())

    assert response.status_code == 503
    assert response.json() == {"detail": "Convite indisponível no momento."}
    assert _convites_da_conta(db_session, admin.conta_id) == []


@pytest.mark.parametrize("frontend_url", ["", "https://frontend-hfrnofw9w-ledgr7.vercel.app"])
def test_frontend_url_ausente_ou_recusada_e_503(
    db_session, provedor, admin, cabecalho_de, monkeypatch, frontend_url
):
    monkeypatch.setattr(settings, "frontend_url", frontend_url)

    response = _convidar(cabecalho_de(admin), _email_novo())

    assert response.status_code == 503
    assert _convites_da_conta(db_session, admin.conta_id) == []
    assert provedor.enviadas == []


# --- logs ----------------------------------------------------------------------


def test_logs_nao_tem_email_nome_nem_token(db_session, provedor, admin, cabecalho_de, caplog):
    email = _email_novo()
    caplog.set_level(logging.INFO)

    token = _convidar_e_pegar_token(cabecalho_de(admin), provedor, email)
    _consultar(token)
    _aceitar(token, nome="Joana Convidada")
    _aceitar(token)

    assert "convite resultado=criado" in caplog.text
    assert "convite resultado=enviado" in caplog.text
    assert "convite_aceite resultado=aceito" in caplog.text
    assert "convite_aceite resultado=token_invalido" in caplog.text
    for segredo in (email, token, "Joana Convidada", admin.nome, "aceitar-convite"):
        assert segredo not in caplog.text
