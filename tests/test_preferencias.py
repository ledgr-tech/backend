"""PATCH /me/preferencias e `preferencias` no GET /me (issue #147): o que a
pessoa dispensou no primeiro uso, guardado no usuário (ver app/api/me.py).

Mesmo padrão de tests/test_me.py.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.me import LIMITE_PREFERENCIAS_POR_USUARIO
from app.core.auth import CABECALHO_EMPRESA
from app.models import Usuario
from app.models.usuario import TELAS_DO_TUTORIAL
from main import app

client = TestClient(app)

LIMITE = int(LIMITE_PREFERENCIAS_POR_USUARIO.split("/")[0])
VAZIAS = {"tutoriais_dispensados": [], "roteiro_dispensado": False}


@pytest.fixture
def cabecalho_de(gerar_token):
    def _cabecalho(usuario) -> dict[str, str]:
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        return {"Authorization": f"Bearer {token}"}

    return _cabecalho


def _patch(cabecalho, corpo):
    return client.patch("/me/preferencias", headers=cabecalho, json=corpo)


def _preferencias_do_me(cabecalho):
    resposta = client.get("/me", headers=cabecalho)
    assert resposta.status_code == 200
    return resposta.json()["preferencias"]


def test_usuario_novo_vem_com_lista_vazia_e_false(criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    assert _preferencias_do_me(cabecalho_de(usuario)) == VAZIAS


def test_tutorial_dispensado_volta_no_get_me(criar_usuario, cabecalho_de, gerar_token):
    usuario = criar_usuario()

    resposta = _patch(cabecalho_de(usuario), {"tutoriais_dispensados": ["visao-geral"]})

    assert resposta.status_code == 200
    assert resposta.json() == {
        "tutoriais_dispensados": ["visao-geral"],
        "roteiro_dispensado": False,
    }
    # Outro navegador: outro token, mesma pessoa.
    outro_navegador = {
        "Authorization": "Bearer " + gerar_token(usuario.conta_id, usuario_id=usuario.id)
    }
    assert _preferencias_do_me(outro_navegador) == resposta.json()


def test_grava_no_usuario(db_session, criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    _patch(
        cabecalho_de(usuario), {"tutoriais_dispensados": ["extratos"], "roteiro_dispensado": True}
    )

    db_session.expire_all()
    linha = db_session.get(Usuario, usuario.id)
    assert linha.tutoriais_dispensados == ["extratos"]
    assert linha.roteiro_dispensado is True


def test_corpo_parcial_nao_apaga_o_outro_campo(criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    _patch(cabecalho, {"tutoriais_dispensados": ["extratos", "historico"]})

    so_roteiro = _patch(cabecalho, {"roteiro_dispensado": True})
    so_tutoriais = _patch(cabecalho, {"tutoriais_dispensados": ["fechamentos"]})

    assert so_roteiro.json() == {
        "tutoriais_dispensados": ["extratos", "historico"],
        "roteiro_dispensado": True,
    }
    assert so_tutoriais.json() == {
        "tutoriais_dispensados": ["fechamentos"],
        "roteiro_dispensado": True,
    }
    assert _preferencias_do_me(cabecalho) == so_tutoriais.json()


def test_o_que_vem_substitui_inclusive_lista_vazia_e_false(criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    _patch(cabecalho, {"tutoriais_dispensados": ["extratos"], "roteiro_dispensado": True})

    resposta = _patch(cabecalho, {"tutoriais_dispensados": [], "roteiro_dispensado": False})

    assert resposta.json() == VAZIAS
    assert _preferencias_do_me(cabecalho) == VAZIAS


def test_corpo_vazio_devolve_o_estado_sem_mudar(criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    guardado = _patch(
        cabecalho, {"tutoriais_dispensados": ["comparacao"], "roteiro_dispensado": True}
    )

    resposta = _patch(cabecalho, {})

    assert resposta.status_code == 200
    assert resposta.json() == guardado.json()
    assert _preferencias_do_me(cabecalho) == guardado.json()


def test_repetidos_saem_na_ordem_da_primeira_ocorrencia(criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = _patch(
        cabecalho_de(usuario),
        {"tutoriais_dispensados": ["historico", "extratos", "historico", "extratos", "comparacao"]},
    )

    assert resposta.json()["tutoriais_dispensados"] == ["historico", "extratos", "comparacao"]


def test_todas_as_telas_validas(criar_usuario, cabecalho_de):
    usuario = criar_usuario()

    resposta = _patch(cabecalho_de(usuario), {"tutoriais_dispensados": list(TELAS_DO_TUTORIAL)})

    assert resposta.status_code == 200
    assert resposta.json()["tutoriais_dispensados"] == list(TELAS_DO_TUTORIAL)


@pytest.mark.parametrize(
    "corpo",
    [
        {"tutoriais_dispensados": ["visao-geral", "tela-que-nao-existe"]},
        {"tutoriais_dispensados": [""]},
        {"tutoriais_dispensados": [1]},
        {"tutoriais_dispensados": "visao-geral"},
        {"tutoriais_dispensados": None},
        {"roteiro_dispensado": None},
        {"roteiro_dispensado": "talvez"},
        {"outro_campo": True},
    ],
)
def test_corpo_invalido_da_422_e_nao_muda_nada(criar_usuario, cabecalho_de, corpo):
    usuario = criar_usuario()
    cabecalho = cabecalho_de(usuario)
    _patch(cabecalho, {"tutoriais_dispensados": ["extratos"]})

    resposta = _patch(cabecalho, corpo)

    assert resposta.status_code == 422
    assert isinstance(resposta.json()["detail"], list)  # formato do FastAPI
    assert _preferencias_do_me(cabecalho) == {
        "tutoriais_dispensados": ["extratos"],
        "roteiro_dispensado": False,
    }


def test_lista_maior_que_o_total_de_telas_da_422(criar_usuario, cabecalho_de):
    usuario = criar_usuario()
    longa = list(TELAS_DO_TUTORIAL) + ["visao-geral"]

    resposta = _patch(cabecalho_de(usuario), {"tutoriais_dispensados": longa})

    assert resposta.status_code == 422
    assert isinstance(resposta.json()["detail"], list)


def test_cada_usuario_ve_e_muda_so_a_propria_preferencia(criar_usuario, cabecalho_de):
    admin = criar_usuario()
    colega = criar_usuario(conta_id=admin.conta_id, papel="analista")
    de_outra_conta = criar_usuario()

    _patch(
        cabecalho_de(admin), {"tutoriais_dispensados": ["visao-geral"], "roteiro_dispensado": True}
    )

    assert _preferencias_do_me(cabecalho_de(colega)) == VAZIAS
    assert _preferencias_do_me(cabecalho_de(de_outra_conta)) == VAZIAS
    _patch(cabecalho_de(colega), {"tutoriais_dispensados": ["extratos"]})
    assert _preferencias_do_me(cabecalho_de(admin)) == {
        "tutoriais_dispensados": ["visao-geral"],
        "roteiro_dispensado": True,
    }


@pytest.mark.parametrize("papel", ["gestor", "analista"])
def test_todos_os_papeis_e_cabecalho_da_empresa_ignorado(criar_usuario, cabecalho_de, papel):
    admin = criar_usuario()
    usuario = criar_usuario(conta_id=admin.conta_id, papel=papel)
    cabecalho = cabecalho_de(usuario) | {CABECALHO_EMPRESA: "nao-e-um-uuid"}

    resposta = _patch(cabecalho, {"roteiro_dispensado": True})

    assert resposta.status_code == 200 and resposta.json()["roteiro_dispensado"] is True


def test_sem_token_e_401():
    assert _patch({}, {"roteiro_dispensado": True}).status_code == 401


def test_rate_limit_por_usuario_nao_afeta_outro_usuario(criar_usuario, cabecalho_de):
    usuario, outro = criar_usuario(), criar_usuario()

    for _ in range(LIMITE):
        assert _patch(cabecalho_de(usuario), {}).status_code == 200
    estourou = _patch(cabecalho_de(usuario), {})

    assert estourou.status_code == 429
    assert "retry-after" in estourou.headers
    assert _patch(cabecalho_de(outro), {}).status_code == 200
