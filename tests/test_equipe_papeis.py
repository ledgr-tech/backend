"""Papéis na equipe (issue #133, parte 2, ADR-019): convite com papel, troca de
papel (`PATCH /empresa/usuarios/{id}`) e transferência do admin
(`POST /empresa/admin/transferencia`).

Matriz: o admin convida gestor ou analista, cancela qualquer convite, muda
papéis e transfere o admin; o gestor convida e cancela só analista; o
analista não mexe em convites. O resto do convite (token, e-mail, aceite) está
em tests/test_convites.py e tests/test_convites_google.py.
"""

import math
import re
import threading
import time
import uuid

import pytest
from conftest import FRONTEND_URL_TESTE, SENHA_USUARIO_TESTE, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.core import auth
from app.core.auth import LOGIN_RECENTE_SEGUNDOS
from app.core.database import SessionLocal
from app.models import Convite, Usuario
from app.services.usuarios import remover_usuario
from main import app

client = TestClient(app)

SO_PELA_TRANSFERENCIA = {"detail": "O admin só muda de papel pela transferência."}
NAO_ENCONTRADO = {"detail": "Usuário não encontrado."}


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario, auth_time=None) -> dict[str, str]:
        kwargs = {} if auth_time is None else {"auth_time": auth_time}
        return cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id, **kwargs))

    return _cabecalho


@pytest.fixture
def equipe(db_session, criar_usuario):
    admin = criar_usuario(nome="Ana Admin")
    gestor = criar_usuario(conta_id=admin.conta_id, nome="Gil Gestor", papel="gestor")
    analista = criar_usuario(conta_id=admin.conta_id, nome="Alan Analista", papel="analista")
    return {"admin": admin, "gestor": gestor, "analista": analista}


def _email() -> str:
    return f"{uuid.uuid4().hex[:12]}@convidado.com"


def _convidar(headers, papel=None, email=None):
    corpo = {"email": email or _email()}
    if papel is not None:
        corpo["papel"] = papel
    return client.post("/empresa/convites", headers=headers, json=corpo)


def _token_do_link(provedor) -> str:
    link = re.search(r"https://\S+", provedor.enviadas[-1].texto).group(0)
    assert link.startswith(f"{FRONTEND_URL_TESTE}/aceitar-convite#token=")
    return link.split("#token=", 1)[1]


def _recarregar(db_session, usuario) -> Usuario:
    db_session.expire_all()
    return db_session.get(Usuario, usuario.id)


def _admins_da_conta(db_session, conta_id) -> int:
    db_session.expire_all()
    return db_session.scalar(
        select(func.count()).where(Usuario.conta_id == conta_id, Usuario.papel == "admin")
    )


# --- convite com papel ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("quem", "papel", "esperado"),
    [
        ("admin", "gestor", 201),
        ("admin", "analista", 201),
        ("admin", None, 201),
        ("gestor", "analista", 201),
        ("gestor", None, 201),
        ("gestor", "gestor", 403),
        ("analista", "analista", 403),
        ("analista", "gestor", 403),
    ],
)
def test_quem_convida_quem(db_session, provedor, equipe, cabecalho_de, quem, papel, esperado):
    resposta = _convidar(cabecalho_de(equipe[quem]), papel)

    assert resposta.status_code == esperado, resposta.text
    if esperado == 201:
        assert resposta.json()["papel"] == (papel or "analista")
        return
    detalhe = (
        "Só o admin da conta pode convidar gestores."
        if quem == "gestor"
        else "Só o admin ou o gestor da conta podem fazer isso."
    )
    assert resposta.json() == {"detail": detalhe}
    assert provedor.enviadas == []


@pytest.mark.parametrize("papel", ["admin", "administrador", ""])
def test_convite_com_papel_admin_ou_invalido_e_422(
    db_session, provedor, equipe, cabecalho_de, papel
):
    assert _convidar(cabecalho_de(equipe["admin"]), papel).status_code == 422


@pytest.mark.parametrize(("quem", "esperado"), [("admin", 200), ("gestor", 200), ("analista", 403)])
def test_quem_lista_os_convites_com_o_papel(
    db_session, provedor, equipe, cabecalho_de, quem, esperado
):
    _convidar(cabecalho_de(equipe["admin"]), "gestor")
    _convidar(cabecalho_de(equipe["gestor"]), "analista")

    resposta = client.get("/empresa/convites", headers=cabecalho_de(equipe[quem]))

    assert resposta.status_code == esperado
    if esperado == 200:
        assert sorted(item["papel"] for item in resposta.json()["itens"]) == [
            "analista",
            "gestor",
        ]


@pytest.mark.parametrize(
    ("quem", "papel_do_convite", "esperado"),
    [
        ("admin", "gestor", 204),
        ("admin", "analista", 204),
        ("gestor", "analista", 204),
        ("gestor", "gestor", 403),
        ("analista", "analista", 403),
    ],
)
def test_quem_cancela_qual_convite(
    db_session, provedor, equipe, cabecalho_de, quem, papel_do_convite, esperado
):
    convite_id = _convidar(cabecalho_de(equipe["admin"]), papel_do_convite).json()["id"]

    resposta = client.delete(f"/empresa/convites/{convite_id}", headers=cabecalho_de(equipe[quem]))

    assert resposta.status_code == esperado
    db_session.expire_all()
    cancelado = db_session.get(Convite, uuid.UUID(convite_id)).cancelado_em is not None
    assert cancelado is (esperado == 204)
    if quem == "gestor" and esperado == 403:
        assert resposta.json() == {
            "detail": "Só o admin da conta pode cancelar convites de gestores."
        }


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_aceite_com_senha_cria_o_usuario_com_o_papel_do_convite(
    db_session, provedor, equipe, cabecalho_de, papel
):
    _convidar(cabecalho_de(equipe["admin"]), papel)
    token = _token_do_link(provedor)

    consulta = client.post("/convites/consultar", json={"token": token})
    aceite = client.post(
        "/convites/aceitar", json={"token": token, "nome": "Nova", "senha": "senha-forte-123"}
    )

    assert consulta.json()["papel"] == papel
    assert aceite.status_code == 201
    novo = db_session.get(Usuario, uuid.UUID(aceite.json()["id"]))
    assert (novo.papel, novo.conta_id) == (papel, equipe["admin"].conta_id)


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_aceite_pelo_google_cria_o_usuario_com_o_papel_do_convite(
    db_session, provedor, google, equipe, cabecalho_de, papel
):
    email = f"{uuid.uuid4().hex[:12]}@gmail.com"
    _convidar(cabecalho_de(equipe["admin"]), papel, email=email)
    token = _token_do_link(provedor)

    aceite = client.post(
        "/convites/aceitar/google",
        json={"token": token, "id_token": google.id_token(email=email)},
    )

    assert aceite.status_code == 201, aceite.text
    assert db_session.get(Usuario, uuid.UUID(aceite.json()["id"])).papel == papel


# --- troca de papel --------------------------------------------------------------------


@pytest.mark.parametrize(("de", "para"), [("gestor", "analista"), ("analista", "gestor")])
def test_admin_troca_o_papel(db_session, equipe, cabecalho_de, de, para):
    alvo = equipe[de]

    resposta = client.patch(
        f"/empresa/usuarios/{alvo.id}",
        headers=cabecalho_de(equipe["admin"]),
        json={"papel": para},
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert (corpo["id"], corpo["papel"], corpo["administrador"]) == (str(alvo.id), para, False)
    assert set(corpo) == {"id", "nome", "email", "papel", "administrador", "criado_em"}
    assert _recarregar(db_session, alvo).papel == para


def test_o_mesmo_papel_de_novo_e_200_sem_mudar_nada(db_session, equipe, cabecalho_de):
    alvo = equipe["gestor"]
    antes = _recarregar(db_session, alvo).atualizado_em

    resposta = client.patch(
        f"/empresa/usuarios/{alvo.id}",
        headers=cabecalho_de(equipe["admin"]),
        json={"papel": "gestor"},
    )

    assert resposta.status_code == 200
    assert resposta.json()["papel"] == "gestor"
    assert _recarregar(db_session, alvo).atualizado_em == antes


@pytest.mark.parametrize("quem", ["gestor", "analista"])
def test_so_o_admin_troca_papel(db_session, equipe, cabecalho_de, quem):
    resposta = client.patch(
        f"/empresa/usuarios/{equipe['analista'].id}",
        headers=cabecalho_de(equipe[quem]),
        json={"papel": "gestor"},
    )

    assert resposta.status_code == 403
    assert resposta.json() == {"detail": "Só o admin da conta pode fazer isso."}
    assert _recarregar(db_session, equipe["analista"]).papel == "analista"


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_o_admin_nao_muda_o_proprio_papel(db_session, equipe, cabecalho_de, papel):
    admin = equipe["admin"]

    resposta = client.patch(
        f"/empresa/usuarios/{admin.id}", headers=cabecalho_de(admin), json={"papel": papel}
    )

    assert resposta.status_code == 409
    assert resposta.json() == SO_PELA_TRANSFERENCIA
    assert _recarregar(db_session, admin).papel == "admin"


def test_papel_admin_na_troca_e_422(db_session, equipe, cabecalho_de):
    resposta = client.patch(
        f"/empresa/usuarios/{equipe['gestor'].id}",
        headers=cabecalho_de(equipe["admin"]),
        json={"papel": "admin"},
    )

    assert resposta.status_code == 422
    assert _recarregar(db_session, equipe["gestor"]).papel == "gestor"


def test_troca_de_papel_de_removido_de_outra_conta_ou_inexistente_e_404(
    db_session, equipe, criar_usuario, cabecalho_de
):
    removido = criar_usuario(conta_id=equipe["admin"].conta_id, papel="analista")
    remover_usuario(db_session, removido)
    db_session.commit()
    de_outra_conta = criar_usuario(conta_id=criar_usuario().conta_id, papel="analista")

    for alvo_id in (removido.id, de_outra_conta.id, uuid.uuid4()):
        resposta = client.patch(
            f"/empresa/usuarios/{alvo_id}",
            headers=cabecalho_de(equipe["admin"]),
            json={"papel": "gestor"},
        )
        assert resposta.status_code == 404
        assert resposta.json() == NAO_ENCONTRADO


# --- transferência do admin -----------------------------------------------------------


def _transferir(headers, usuario_id, senha_atual=SENHA_USUARIO_TESTE):
    corpo = {"usuario_id": str(usuario_id)}
    if senha_atual is not None:
        corpo["senha_atual"] = senha_atual
    return client.post("/empresa/admin/transferencia", headers=headers, json=corpo)


@pytest.mark.parametrize("destino", ["gestor", "analista"])
def test_transferencia_troca_os_dois_papeis(db_session, equipe, cabecalho_de, destino):
    admin, novo = equipe["admin"], equipe[destino]

    resposta = _transferir(cabecalho_de(admin), novo.id)

    assert resposta.status_code == 200, resposta.text
    papeis = {item["id"]: item["papel"] for item in resposta.json()["itens"]}
    assert papeis[str(admin.id)] == "gestor"
    assert papeis[str(novo.id)] == "admin"
    assert _recarregar(db_session, admin).papel == "gestor"
    assert _recarregar(db_session, novo).papel == "admin"
    assert _admins_da_conta(db_session, admin.conta_id) == 1


def test_depois_da_transferencia_o_papel_vale_na_hora_sem_novo_login(
    db_session, equipe, cabecalho_de
):
    admin, novo = equipe["admin"], equipe["analista"]
    headers_antigo, headers_novo = cabecalho_de(admin), cabecalho_de(novo)

    assert _transferir(headers_antigo, novo.id).status_code == 200

    # Ninguém foi deslogado: o mesmo token vale, com o papel lido do banco.
    assert client.get("/me", headers=headers_antigo).json()["papel"] == "gestor"
    assert client.get("/me", headers=headers_novo).json()["papel"] == "admin"
    plano = client.post("/empresa/plano", headers=headers_antigo, json={"plano": "padrao"})
    assert plano.status_code == 403
    assert (
        client.post("/empresa/plano", headers=headers_novo, json={"plano": "padrao"}).status_code
        == 200
    )


@pytest.mark.parametrize("quem", ["gestor", "analista"])
def test_so_o_admin_transfere(db_session, equipe, cabecalho_de, quem):
    resposta = _transferir(cabecalho_de(equipe[quem]), equipe[quem].id)

    assert resposta.status_code == 403
    assert _admins_da_conta(db_session, equipe["admin"].conta_id) == 1


@pytest.mark.parametrize("senha_atual", ["senha-errada-1", "", None])
def test_senha_errada_ou_ausente_e_400(db_session, equipe, cabecalho_de, senha_atual):
    resposta = _transferir(cabecalho_de(equipe["admin"]), equipe["gestor"].id, senha_atual)

    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Senha atual incorreta."}
    assert _recarregar(db_session, equipe["admin"]).papel == "admin"


def test_conta_do_google_com_login_recente_transfere(db_session, criar_usuario, cabecalho_de):
    admin = criar_usuario(com_senha=False)
    destino = criar_usuario(conta_id=admin.conta_id, papel="analista")

    resposta = _transferir(cabecalho_de(admin), destino.id, senha_atual=None)

    assert resposta.status_code == 200
    assert _recarregar(db_session, destino).papel == "admin"


def test_conta_do_google_sem_login_recente_e_409(db_session, criar_usuario, cabecalho_de):
    admin = criar_usuario(com_senha=False)
    destino = criar_usuario(conta_id=admin.conta_id, papel="analista")
    antigo = math.floor(auth._agora().timestamp()) - LOGIN_RECENTE_SEGUNDOS - 1

    resposta = _transferir(cabecalho_de(admin, auth_time=antigo), destino.id, senha_atual=None)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": "Entre de novo com o Google para confirmar a desativação."}
    assert _recarregar(db_session, admin).papel == "admin"


def test_transferir_para_si_mesmo_e_409(db_session, equipe, cabecalho_de):
    admin = equipe["admin"]

    resposta = _transferir(cabecalho_de(admin), admin.id)

    assert resposta.status_code == 409
    assert _recarregar(db_session, admin).papel == "admin"


def test_destino_removido_de_outra_conta_ou_inexistente_e_404(
    db_session, equipe, criar_usuario, cabecalho_de
):
    admin = equipe["admin"]
    removido = criar_usuario(conta_id=admin.conta_id, papel="gestor")
    remover_usuario(db_session, removido)
    db_session.commit()
    de_outra_conta = criar_usuario(conta_id=criar_usuario().conta_id, papel="gestor")

    for destino_id in (removido.id, de_outra_conta.id, uuid.uuid4()):
        resposta = _transferir(cabecalho_de(admin), destino_id)
        assert resposta.status_code == 404
        assert resposta.json() == NAO_ENCONTRADO

    assert _recarregar(db_session, admin).papel == "admin"
    assert _recarregar(db_session, de_outra_conta).papel == "gestor"


def test_duas_transferencias_simultaneas_so_uma_vence(db_session, equipe, cabecalho_de):
    """As duas passam pela confirmação e esperam a linha do admin, travada por
    outra conexão. Solta a trava: a primeira transfere, a segunda encontra quem
    pediu já como gestor e recebe 409. A conta termina com um admin só."""
    admin = equipe["admin"]
    headers = cabecalho_de(admin)
    respostas: dict[str, int] = {}

    trava = SessionLocal()
    trava.execute(text("SELECT id FROM usuarios WHERE id = :id FOR UPDATE"), {"id": admin.id})

    def _pedir(destino):
        respostas[destino] = _transferir(headers, equipe[destino].id).status_code

    threads = [threading.Thread(target=_pedir, args=(d,)) for d in ("gestor", "analista")]
    try:
        for thread in threads:
            thread.start()
        time.sleep(1.5)  # as duas chegam à trava
        assert respostas == {}
    finally:
        trava.rollback()
        trava.close()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(respostas.values()) == [200, 409]
    assert _admins_da_conta(db_session, admin.conta_id) == 1
    assert _recarregar(db_session, admin).papel == "gestor"
    vencedor = next(destino for destino, codigo in respostas.items() if codigo == 200)
    assert _recarregar(db_session, equipe[vencedor]).papel == "admin"


def test_depois_da_transferencia_o_antigo_admin_pode_ser_removido_e_desativar(
    db_session, equipe, cabecalho_de
):
    admin, novo = equipe["admin"], equipe["gestor"]
    assert _transferir(cabecalho_de(admin), novo.id).status_code == 200

    desativar = client.request(
        "DELETE", "/me", headers=cabecalho_de(admin), json={"senha_atual": SENHA_USUARIO_TESTE}
    )

    assert desativar.status_code == 204
    assert _admins_da_conta(db_session, admin.conta_id) == 1


def test_o_novo_admin_nao_pode_ser_removido_nem_rebaixado(db_session, equipe, cabecalho_de):
    admin, novo = equipe["admin"], equipe["gestor"]
    assert _transferir(cabecalho_de(admin), novo.id).status_code == 200
    headers_novo = cabecalho_de(novo)

    remover = client.delete(f"/empresa/usuarios/{novo.id}", headers=headers_novo)
    rebaixar = client.patch(
        f"/empresa/usuarios/{novo.id}", headers=headers_novo, json={"papel": "gestor"}
    )
    remover_pelo_antigo = client.delete(f"/empresa/usuarios/{novo.id}", headers=cabecalho_de(admin))

    assert remover.status_code == 409
    assert rebaixar.status_code == 409
    assert rebaixar.json() == SO_PELA_TRANSFERENCIA
    assert remover_pelo_antigo.status_code == 403
    assert _recarregar(db_session, novo).papel == "admin"


# --- remoção e DELETE /me ------------------------------------------------------------


@pytest.mark.parametrize("quem", ["gestor", "analista"])
def test_so_o_admin_remove_usuario(db_session, equipe, cabecalho_de, quem):
    alvo = equipe["analista"] if quem == "gestor" else equipe["gestor"]

    resposta = client.delete(f"/empresa/usuarios/{alvo.id}", headers=cabecalho_de(equipe[quem]))

    assert resposta.status_code == 403
    assert _recarregar(db_session, alvo).removido_em is None


def test_admin_com_equipe_e_orientado_a_transferir(db_session, equipe, cabecalho_de):
    resposta = client.request(
        "DELETE",
        "/me",
        headers=cabecalho_de(equipe["admin"]),
        json={"senha_atual": SENHA_USUARIO_TESTE},
    )

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": "Transfira o papel de admin antes de desativar a sua conta."
    }
