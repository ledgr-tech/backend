"""Testes de GET /me (issue #81): dados da conta pro cabeçalho do front —
razão social e CNPJ da conta, métodos de login e, desde a issue #133, o
`papel`, o `conta_id` e as empresas clientes da conta, com `administrador` e
`empresa_id` obsoletos mantidos (ver app/api/me.py).

Mesmo padrão de tests/test_troca_senha.py: rota por usuário, autenticação e
rate limit testados com as fixtures de conftest.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, update

from app.api.me import LIMITE_CONSULTA_ME_POR_USUARIO
from app.core import conta_inativa
from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from app.models import Conta
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
        # Conta sem cidade e UF informadas (issue #184).
        "cidade": None,
        "uf": None,
        "empresas": [
            {"id": str(empresa.id), "razao_social": empresa.razao_social, "cnpj": empresa.cnpj}
        ],
        "metodos_login": ["senha"],
        # Conta nova, sem plano escolhido (issue #122).
        "plano": None,
        "teste_termina_em": None,
        "situacao": "sem_plano",
        "cobranca_habilitada": False,
        # Ainda não assinou pela Stripe (issues #166 e #167).
        "assinatura": None,
        # Sem limite configurado (issue #133): só o uso.
        "limites": {
            "empresas": {"usadas": 1, "maximo": None},
            "usuarios": {"usados": 1, "maximo": None},
            "conciliacoes": {"usadas": 0, "maximo": None},
        },
        # Usuário novo, nada dispensado (issue #147).
        "preferencias": {"tutoriais_dispensados": [], "roteiro_dispensado": False},
        # Flag desligada e conta sem plano (issue #139).
        "guarda_arquivo_original": False,
        # Conta ativa (issues #156 e #214).
        "conta_inativa": False,
        "conta_inativada_em": None,
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


# --- conta inativa (issues #156 e #214) ---------------------------------------


MOTIVO = "Inadimplência há 90 dias, combinado com o financeiro"


def _inativar_no_banco(db_session, conta_id, motivo=MOTIVO):
    db_session.execute(
        update(Conta)
        .where(Conta.id == conta_id)
        .values(inativada_em=func.now(), motivo_inativacao=motivo)
    )
    db_session.commit()


def _inativada_em_gravada(db_session, conta_id) -> datetime:
    db_session.expire_all()
    return db_session.get(Conta, conta_id).inativada_em


def test_conta_ativa_diz_que_nao_esta_inativa(cliente):
    c = cliente()

    corpo = _me(c.headers["admin"]).json()

    assert corpo["conta_inativa"] is False
    assert corpo["conta_inativada_em"] is None


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_conta_inativa_diz_que_esta_e_quando_em_utc(db_session, cliente, papel):
    c = cliente()
    _inativar_no_banco(db_session, c.conta_id)
    gravada = _inativada_em_gravada(db_session, c.conta_id)

    response = _me(c.headers[papel])

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["conta_inativa"] is True
    em = datetime.fromisoformat(corpo["conta_inativada_em"])
    assert em == gravada
    assert em.utcoffset() == timedelta(0)  # UTC explícito, como teste_termina_em
    assert corpo["conta_inativada_em"].endswith(("Z", "+00:00"))


def test_conta_inativa_nao_depende_da_empresa_ativa(db_session, cliente):
    c = cliente()
    _inativar_no_banco(db_session, c.conta_id)
    headers = {**c.headers["gestor"], CABECALHO_EMPRESA: "nao-e-uuid"}

    response = _me(headers)

    assert response.status_code == 200
    assert response.json()["conta_inativa"] is True


def test_a_resposta_nunca_expoe_o_motivo_da_inativacao(db_session, cliente):
    c = cliente()
    _inativar_no_banco(db_session, c.conta_id)

    for papel in ("admin", "gestor", "analista"):
        response = _me(c.headers[papel])

        assert response.status_code == 200
        assert "motivo_inativacao" not in response.json()
        assert not [chave for chave in response.json() if "motivo" in chave]
        assert MOTIVO not in response.text
        assert "Inadimplência" not in response.text


def test_me_segue_a_inativacao_e_a_reativacao_feitas_pelo_admin(db_session, operador, cliente):
    """Pelo caminho do painel (`POST /admin/contas/{id}/inativar` e
    `/reativar`): sem cache, o `/me` seguinte já reflete, e nenhuma das
    consultas é recusada."""
    o = operador()
    c = cliente()
    headers = c.headers["admin"]

    antes = _me(headers)
    inativou = client.post(
        f"/admin/contas/{c.conta_id}/inativar", headers=o.headers, json={"motivo": MOTIVO}
    )
    inativa = _me(headers)
    reativou = client.post(
        f"/admin/contas/{c.conta_id}/reativar", headers=o.headers, json={"motivo": "Pagou"}
    )
    depois = _me(headers)
    de_novo = client.post(
        f"/admin/contas/{c.conta_id}/inativar", headers=o.headers, json={"motivo": MOTIVO}
    )
    inativa_de_novo = _me(headers)

    assert (inativou.status_code, reativou.status_code, de_novo.status_code) == (200, 200, 200)
    assert [r.status_code for r in (antes, inativa, depois, inativa_de_novo)] == [200] * 4
    assert (antes.json()["conta_inativa"], antes.json()["conta_inativada_em"]) == (False, None)
    assert inativa.json()["conta_inativa"] is True
    # A data do /me é a que o painel mostra para a inativação.
    assert datetime.fromisoformat(inativa.json()["conta_inativada_em"]) == datetime.fromisoformat(
        inativou.json()["inativacao"]["em"]
    )
    assert (depois.json()["conta_inativa"], depois.json()["conta_inativada_em"]) == (False, None)
    assert inativa_de_novo.json()["conta_inativa"] is True
    assert datetime.fromisoformat(
        inativa_de_novo.json()["conta_inativada_em"]
    ) >= datetime.fromisoformat(inativa.json()["conta_inativada_em"])
    assert all(MOTIVO not in r.text for r in (inativa, inativa_de_novo))


def test_me_responde_200_com_a_conta_inativa_e_a_escrita_continua_403(db_session, cliente):
    """Regressão da #156: a leitura é liberada, a escrita segue barrada."""
    c = cliente()
    _inativar_no_banco(db_session, c.conta_id)

    leitura = _me(c.headers["admin"])
    escrita = client.post(
        "/empresas",
        headers=c.headers["admin"],
        json={"cnpj": "11222333000181", "razao_social": "Nova"},
    )

    assert leitura.status_code == 200
    assert leitura.json()["conta_inativa"] is True
    assert (escrita.status_code, escrita.json()) == (403, conta_inativa.corpo())
    assert escrita.json()["codigo"] == "conta_inativa"


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


def test_me_com_endpoint_do_r2_invalido_continua_200(
    db_session, criar_usuario, cabecalho_de, monkeypatch
):
    # Revisão final (I1): uma variável do R2 mal digitada em produção, com a
    # flag desligada, não pode tirar o /me do ar.
    monkeypatch.setattr(settings, "arquivo_original_r2_endpoint", "abc.r2.cloudflarestorage.com")
    monkeypatch.setattr(settings, "arquivo_original_r2_bucket", "ledgr-extratos")
    monkeypatch.setattr(settings, "arquivo_original_r2_access_key_id", "id")
    monkeypatch.setattr(settings, "arquivo_original_r2_secret_access_key", "segredo")
    monkeypatch.setattr(
        settings, "arquivo_original_chave", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    )
    usuario = criar_usuario()

    response = _me(cabecalho_de(usuario))

    assert response.status_code == 200
    assert response.json()["guarda_arquivo_original"] is False


@pytest.mark.parametrize(
    ("habilitada", "chave", "esperado"),
    [
        (True, "sk_test_chave", True),
        (True, "", False),
        (True, "   ", False),
        (False, "sk_test_chave", False),
        (False, "", False),
    ],
)
def test_sinal_de_cobranca(criar_usuario, cabecalho_de, monkeypatch, habilitada, chave, esperado):
    monkeypatch.setattr(settings, "stripe_habilitado", habilitada)
    monkeypatch.setattr(settings, "stripe_secret_key", chave)
    resposta = _me(cabecalho_de(criar_usuario()))
    assert resposta.status_code == 200
    assert resposta.json()["cobranca_habilitada"] is esperado
