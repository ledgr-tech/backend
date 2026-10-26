"""Testes de GET /me (issue #81): dados da conta pro cabeçalho do front —
razão social e CNPJ da conta, métodos de login e, desde a issue #133, o
`papel`, o `conta_id` e as empresas clientes da conta, com `administrador` e
`empresa_id` obsoletos mantidos (ver app/api/me.py).

Mesmo padrão de tests/test_troca_senha.py: rota por usuário, autenticação e
rate limit testados com as fixtures de conftest.py.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.me import LIMITE_CONSULTA_ME_POR_USUARIO
from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from main import app

client = TestClient(app)

LIMITE = int(LIMITE_CONSULTA_ME_POR_USUARIO.split("/")[0])


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario, conta_id=None) -> dict[str, str]:
        token = gerar_token(conta_id or usuario.conta_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _me(cabecalho):
    return client.get("/me", headers=cabecalho)


# --- sucesso ---------------------------------------------------------------


def test_200_com_todos_os_campos_para_usuario_com_senha(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    conta = usuario.conta
    (empresa,) = conta.empresas

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    corpo = response.json()
    assert corpo == {
        "id": str(usuario.id),
        "conta_id": str(conta.id),
        "empresa_id": str(conta.id),  # obsoleto: o id da conta (issue #133)
        "nome": usuario.nome,
        "email": usuario.email,
        "papel": "admin",
        "administrador": True,  # obsoleto: papel == "admin"
        "razao_social": conta.razao_social,
        "cnpj": conta.cnpj,
        "empresas": [
            {"id": str(empresa.id), "razao_social": empresa.razao_social, "cnpj": empresa.cnpj}
        ],
        "metodos_login": ["senha"],
        # Conta nova, sem plano escolhido (issue #122).
        "plano": None,
        "teste_termina_em": None,
        "situacao": "sem_plano",
        # Sem limite configurado (issue #133): só o uso.
        "limites": {
            "empresas": {"usadas": 1, "maximo": None},
            "usuarios": {"usados": 1, "maximo": None},
        },
        # Usuário novo, nada dispensado (issue #147).
        "preferencias": {"tutoriais_dispensados": [], "roteiro_dispensado": False},
        # Flag desligada e conta sem plano (issue #139).
        "guarda_arquivo_original": False,
    }
    assert len(corpo["cnpj"]) == 14
    assert not corpo["cnpj"].isalpha()  # sem máscara (sem pontuação)
    assert "." not in corpo["cnpj"] and "/" not in corpo["cnpj"] and "-" not in corpo["cnpj"]


def test_conta_sem_senha_devolve_metodo_google(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario(com_senha=False)

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["metodos_login"] == ["google"]


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_quem_nao_e_admin_ve_o_proprio_papel(db_session, criar_usuario, cabecalho_de, papel):
    administrador = criar_usuario()
    convidado = criar_usuario(conta_id=administrador.conta_id, papel=papel)

    response = _me(cabecalho_de(convidado))

    assert response.status_code == 200
    corpo = response.json()
    assert (corpo["papel"], corpo["administrador"]) == (papel, False)
    assert corpo["conta_id"] == str(administrador.conta_id)


def test_empresas_da_conta_em_ordem_de_razao_social_e_cabecalho_ignorado(
    db_session, criar_usuario, criar_empresa, cabecalho_de
):
    """Issue #133: todas as empresas da conta, pela razão social, para o
    seletor; o `/me` é da conta e não confere `X-Ledgr-Empresa`."""
    usuario = criar_usuario()
    zeta = criar_empresa("Zeta Comércio", conta_id=usuario.conta_id)
    alfa = criar_empresa("Alfa Serviços", conta_id=usuario.conta_id)
    criar_empresa("Outra conta")
    headers = {**cabecalho_de(usuario), CABECALHO_EMPRESA: "nao-e-uuid"}

    response = _me(headers)

    assert response.status_code == 200
    nomes = [empresa["razao_social"] for empresa in response.json()["empresas"]]
    assert nomes == ["Alfa Serviços", "Empresa de teste", "Zeta Comércio"]
    ids = {empresa["id"] for empresa in response.json()["empresas"]}
    assert {str(alfa.id), str(zeta.id)} <= ids


def test_conta_com_senha_e_google_devolve_os_dois_na_ordem(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    usuario.google_sub = f"google-{uuid.uuid4().hex}"
    db_session.commit()

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["metodos_login"] == ["senha", "google"]


def test_resposta_nao_expoe_hashes(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    usuario.google_sub = f"google-{uuid.uuid4().hex}"
    db_session.commit()

    response = _me(cabecalho_de(usuario))

    corpo = response.json()
    assert "senha_hash" not in corpo
    assert "google_sub" not in corpo
    assert usuario.senha_hash not in response.text
    assert usuario.google_sub not in response.text


# --- autenticação por usuário -----------------------------------------------


def test_sem_token_e_401():
    assert _me({}).status_code == 401


def test_token_assinado_com_outro_segredo_e_401(db_session, criar_usuario, gerar_token):
    usuario = criar_usuario()
    token = gerar_token(usuario.conta_id, secret="outro-segredo-qualquer", usuario_id=usuario.id)

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_sub_que_nao_e_uuid_e_401(db_session, criar_empresa, gerar_token):
    token = gerar_token(criar_empresa().conta_id, usuario_id="nao-e-uuid")

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_sub_de_usuario_inexistente_e_401(db_session, criar_empresa, gerar_token):
    empresa = criar_empresa()
    token = gerar_token(empresa.conta_id, usuario_id=uuid.uuid4())

    assert _me({"Authorization": f"Bearer {token}"}).status_code == 401


def test_usuario_com_conta_diferente_da_do_token_e_401(
    db_session, criar_usuario, criar_empresa, cabecalho_de
):
    usuario = criar_usuario()
    outra_conta = criar_empresa().conta_id

    response = _me(cabecalho_de(usuario, conta_id=outra_conta))

    assert response.status_code == 401


def test_token_com_o_id_da_empresa_no_lugar_do_da_conta_e_401(
    db_session, criar_usuario, cabecalho_de
):
    """O id da empresa cliente não é o da conta (issue #133)."""
    usuario = criar_usuario()
    (empresa,) = usuario.conta.empresas

    assert _me(cabecalho_de(usuario, conta_id=empresa.id)).status_code == 401


# --- isolamento entre contas ------------------------------------------------


def test_cada_usuario_recebe_so_os_dados_da_propria_conta(db_session, criar_usuario, cabecalho_de):
    usuario_a, usuario_b = criar_usuario(), criar_usuario()

    corpo_a = _me(cabecalho_de(usuario_a)).json()
    corpo_b = _me(cabecalho_de(usuario_b)).json()

    assert corpo_a["conta_id"] != corpo_b["conta_id"]
    assert corpo_a["razao_social"] == usuario_a.conta.razao_social
    assert corpo_b["razao_social"] == usuario_b.conta.razao_social
    assert {e["id"] for e in corpo_a["empresas"]}.isdisjoint(e["id"] for e in corpo_b["empresas"])


def test_token_com_sub_de_a_e_conta_de_b_e_401(db_session, criar_usuario, cabecalho_de):
    usuario_a, usuario_b = criar_usuario(), criar_usuario()

    response = _me(cabecalho_de(usuario_a, conta_id=usuario_b.conta_id))

    assert response.status_code == 401


# --- rate limit por usuário --------------------------------------------------


def test_rate_limit_por_usuario_nao_afeta_outro_usuario(db_session, criar_usuario, cabecalho_de):
    alvo, outro = criar_usuario(), criar_usuario()

    respostas = [_me(cabecalho_de(alvo)) for _ in range(LIMITE + 1)]

    assert respostas[-2].status_code == 200
    assert respostas[-1].status_code == 429
    assert _me(cabecalho_de(outro)).status_code == 200


# --- arquivo original do extrato (issue #139, ADR-023) ---------------------------


@pytest.mark.parametrize(
    "plano, ligar_servico, flag, esperado",
    [
        ("avancado", True, True, True),
        ("padrao", True, True, False),
        ("avancado", True, False, False),
        # Flag ligada sem as variáveis do R2: nada é guardado.
        ("avancado", False, True, False),
    ],
)
def test_me_guarda_arquivo_original(
    db_session,
    criar_usuario,
    cabecalho_de,
    request,
    monkeypatch,
    plano,
    ligar_servico,
    flag,
    esperado,
):
    if ligar_servico:
        request.getfixturevalue("arquivo_original")
    monkeypatch.setattr(settings, "arquivo_original_habilitado", flag)
    usuario = criar_usuario()
    # O CHECK de `contas` exige o plano e o fim do teste juntos.
    usuario.conta.plano = plano
    usuario.conta.teste_termina_em = datetime(2099, 1, 1, tzinfo=UTC)
    db_session.commit()

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["guarda_arquivo_original"] is esperado
