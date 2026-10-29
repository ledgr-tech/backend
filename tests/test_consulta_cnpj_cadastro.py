"""Verificação do CNPJ na Receita no cadastro (issue #119): `POST
/register/verificar`, `POST /register` e `POST /register/google` com a consulta
ligada, a BrasilAPI num `httpx.MockTransport` (nada sai para a internet) e o
cache de 24 horas. A consulta desligada, o padrão, está coberta pelos testes
de cada rota (tests/test_verificar_cadastro.py, tests/test_auth_endpoints.py e
tests/test_login_google.py) e por `test_consulta_desligada_*` abaixo.
"""

import uuid

import httpx
import pytest
from conftest import _gerar_cnpj, _gerar_cnpj_alfanumerico, apagar_contas_por_cnpj
from fastapi.testclient import TestClient
from test_consulta_cnpj import NAO_ENCONTRADO_BRASILAPI, corpo_brasilapi

from app.core.database import engine
from app.models import Empresa
from app.services.consulta_cnpj import ConsultaBrasilAPI, obter_consulta_cnpj_dependencia
from main import app

client = TestClient(app)

BASE = "https://brasilapi.test/api/cnpj/v1"
RAZAO = "EMPRESA DE TESTE LTDA"
ROTAS_DE_CADASTRO = ["/register", "/register/google"]


class ReceitaFalsa:
    """A BrasilAPI de mentira: devolve `resposta` (ou levanta, se for exceção)
    e anota cada pedido e as conexões do banco em uso durante ele."""

    def __init__(self) -> None:
        self.resposta: httpx.Response | type[Exception] = httpx.Response(200)
        self.pedidos: list[httpx.Request] = []
        self.conexoes_em_uso: list[int] = []

    def responder_ativa(self, cnpj: str, situacao: str = "ATIVA") -> None:
        self.resposta = httpx.Response(
            200, json=corpo_brasilapi(situacao=situacao, razao=RAZAO, cnpj=cnpj)
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        self.conexoes_em_uso.append(engine.pool.checkedout())
        if isinstance(self.resposta, type):
            raise self.resposta("falhou", request=request)
        return self.resposta


@pytest.fixture
def receita():
    falsa = ReceitaFalsa()
    consulta = ConsultaBrasilAPI(
        base_url=BASE, cliente_http=httpx.Client(transport=httpx.MockTransport(falsa.handler))
    )
    app.dependency_overrides[obter_consulta_cnpj_dependencia] = lambda: consulta
    yield falsa
    app.dependency_overrides.pop(obter_consulta_cnpj_dependencia, None)


@pytest.fixture
def cadastrar(db_session, google):
    """`cadastrar(rota, cnpj)` pelo `/register` ou pelo `/register/google`, com
    e-mail único; no fim, apaga só o que esses cadastros criaram."""
    cnpjs: list[str] = []

    def _cadastrar(rota: str, cnpj: str) -> httpx.Response:
        cnpjs.append(cnpj.upper())
        email = f"{uuid.uuid4().hex[:12]}@teste.com"
        if rota == "/register":
            corpo = {
                "nome": "Maria",
                "email": email,
                "senha": "senha-forte-123",
                "razao_social": "Empresa digitada",
                "cnpj": cnpj,
            }
        else:
            corpo = {
                "id_token": google.id_token(email=email),
                "razao_social": "Empresa digitada",
                "cnpj": cnpj,
            }
        return client.post(rota, json=corpo)

    yield _cadastrar

    db_session.rollback()
    apagar_contas_por_cnpj(db_session, cnpjs)
    db_session.commit()


def _verificar(cnpj: str) -> httpx.Response:
    return client.post(
        "/register/verificar", json={"email": f"{uuid.uuid4().hex[:12]}@teste.com", "cnpj": cnpj}
    )


def _empresa(db_session, cnpj: str) -> Empresa | None:
    db_session.expire_all()
    return db_session.query(Empresa).filter_by(cnpj=cnpj).one_or_none()


# Respostas da API que não dão para saber a situação: viram "nao_verificado"
# e não bloqueiam o cadastro.
INDISPONIVEIS = {
    "timeout": httpx.ReadTimeout,
    "erro_de_rede": httpx.ConnectError,
    "5xx": httpx.Response(503, json={"type": "service_unavailable"}),
    "429": httpx.Response(429, json={"message": "Too many requests"}),
    "corpo_invalido": httpx.Response(200, json={"inesperado": True}),
    "formato_recusado": httpx.Response(400, json={"type": "bad_request"}),
}


# --- POST /register/verificar ------------------------------------------------------


def test_verificar_ativa_devolve_situacao_e_razao_social(db_session, receita):
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj)

    resposta = _verificar(cnpj)

    assert resposta.status_code == 200
    assert resposta.json() == {
        "email_disponivel": True,
        "cnpj_disponivel": True,
        "cnpj_situacao": "ativa",
        "razao_social": RAZAO,
    }
    assert str(receita.pedidos[0].url) == f"{BASE}/{cnpj}"


def test_verificar_inativa_tambem_devolve_a_razao_social(db_session, receita):
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj, situacao="BAIXADA")

    corpo = _verificar(cnpj).json()

    assert (corpo["cnpj_situacao"], corpo["razao_social"]) == ("inativa", RAZAO)


def test_verificar_nao_encontrado(db_session, receita):
    receita.resposta = httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI)

    corpo = _verificar(_gerar_cnpj()).json()

    assert (corpo["cnpj_situacao"], corpo["razao_social"]) == ("nao_encontrado", None)


@pytest.mark.parametrize("resposta", INDISPONIVEIS.values(), ids=INDISPONIVEIS.keys())
def test_verificar_indisponivel_vira_nao_verificado(db_session, receita, resposta):
    receita.resposta = resposta

    resposta_http = _verificar(_gerar_cnpj())

    assert resposta_http.status_code == 200
    corpo = resposta_http.json()
    assert (corpo["cnpj_situacao"], corpo["razao_social"]) == ("nao_verificado", None)
    assert len(receita.pedidos) == 1


def test_verificar_cnpj_que_ja_tem_empresa_nao_consulta(db_session, receita, criar_empresa):
    empresa = criar_empresa()

    corpo = _verificar(empresa.cnpj).json()

    assert corpo["cnpj_disponivel"] is False
    assert (corpo["cnpj_situacao"], corpo["razao_social"]) == ("nao_verificado", None)
    assert receita.pedidos == []


# --- POST /register e POST /register/google ---------------------------------------


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
def test_cadastro_com_cnpj_ativo_marca_a_empresa_como_verificada(
    db_session, receita, cadastrar, rota
):
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj)

    resposta = cadastrar(rota, cnpj)

    assert resposta.status_code == 201
    empresa = _empresa(db_session, cnpj)
    assert empresa.cnpj_verificado_em is not None
    assert empresa.cnpj_verificado_em.tzinfo is not None
    # A razão social digitada fica; a da Receita não é gravada.
    assert empresa.razao_social == "Empresa digitada"
    # Nenhuma conexão do banco presa durante a chamada externa.
    assert receita.conexoes_em_uso == [0]


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
@pytest.mark.parametrize(
    ("situacao", "mensagem"),
    [
        ("nao_encontrado", "CNPJ não encontrado na Receita Federal."),
        ("inativa", "CNPJ não está ativo na Receita Federal."),
    ],
)
def test_cadastro_com_cnpj_nao_encontrado_ou_inativo_da_422_no_campo_cnpj(
    db_session, receita, cadastrar, rota, situacao, mensagem
):
    cnpj = _gerar_cnpj()
    if situacao == "inativa":
        receita.responder_ativa(cnpj, situacao="INAPTA")
    else:
        receita.resposta = httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI)

    resposta = cadastrar(rota, cnpj)

    assert resposta.status_code == 422
    erro = resposta.json()["detail"][0]
    assert erro["loc"] == ["body", "cnpj"]
    assert erro["msg"] == mensagem
    assert _empresa(db_session, cnpj) is None


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
@pytest.mark.parametrize("resposta", INDISPONIVEIS.values(), ids=INDISPONIVEIS.keys())
def test_cadastro_com_receita_indisponivel_cria_a_empresa_nao_verificada(
    db_session, receita, cadastrar, rota, resposta
):
    cnpj = _gerar_cnpj()
    receita.resposta = resposta

    resposta_http = cadastrar(rota, cnpj)

    assert resposta_http.status_code == 201
    assert _empresa(db_session, cnpj).cnpj_verificado_em is None
    assert len(receita.pedidos) == 1


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
def test_cadastro_com_email_em_uso_nao_consulta_a_receita(
    db_session, receita, cadastrar, criar_usuario, rota, google
):
    usuario = criar_usuario()
    cnpj = _gerar_cnpj()
    if rota == "/register":
        corpo = {
            "nome": "Maria",
            "email": usuario.email,
            "senha": "senha-forte-123",
            "razao_social": "Empresa",
            "cnpj": cnpj,
        }
    else:
        corpo = {
            "id_token": google.id_token(email=usuario.email),
            "razao_social": "Empresa",
            "cnpj": cnpj,
        }

    assert client.post(rota, json=corpo).status_code == 409
    assert receita.pedidos == []


# --- dígito verificador, cache, alfanumérico e consulta desligada ------------------


@pytest.mark.parametrize("rota", ["/register/verificar", *ROTAS_DE_CADASTRO])
@pytest.mark.parametrize(
    "cnpj", ["12345678000190", "12ABC34501DE36", "12ABC34501DE3A", "00000000000000"]
)
def test_cnpj_com_digito_invalido_e_recusado_sem_chamar_a_api(
    db_session, receita, cadastrar, rota, cnpj
):
    if rota == "/register/verificar":
        resposta = _verificar(cnpj)
    else:
        resposta = cadastrar(rota, cnpj)

    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["loc"] == ["body", "cnpj"]
    assert receita.pedidos == []


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
def test_verificar_e_depois_cadastrar_faz_uma_consulta_so(db_session, receita, cadastrar, rota):
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj)

    assert _verificar(cnpj).json()["cnpj_situacao"] == "ativa"
    assert cadastrar(rota, cnpj).status_code == 201

    assert len(receita.pedidos) == 1
    assert _empresa(db_session, cnpj).cnpj_verificado_em is not None


def test_nao_encontrado_em_cache_barra_o_cadastro_sem_consultar_de_novo(
    db_session, receita, cadastrar
):
    cnpj = _gerar_cnpj()
    receita.resposta = httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI)

    _verificar(cnpj)
    resposta = cadastrar("/register", cnpj)

    assert resposta.status_code == 422
    assert len(receita.pedidos) == 1


def test_indisponivel_nao_fica_em_cache(db_session, receita, cadastrar):
    cnpj = _gerar_cnpj()
    receita.resposta = httpx.ReadTimeout

    assert _verificar(cnpj).json()["cnpj_situacao"] == "nao_verificado"
    receita.responder_ativa(cnpj)
    assert cadastrar("/register", cnpj).status_code == 201

    assert len(receita.pedidos) == 2
    assert _empresa(db_session, cnpj).cnpj_verificado_em is not None


@pytest.mark.parametrize("rota", ["/register/verificar", *ROTAS_DE_CADASTRO])
def test_cnpj_alfanumerico_recusado_pela_api_vira_nao_verificado_e_nao_bloqueia(
    db_session, receita, cadastrar, rota
):
    cnpj = _gerar_cnpj_alfanumerico()
    receita.resposta = httpx.Response(
        400, json={"message": "CNPJ deve conter 14 dígitos.", "type": "bad_request"}
    )

    if rota == "/register/verificar":
        assert _verificar(cnpj).json()["cnpj_situacao"] == "nao_verificado"
    else:
        assert cadastrar(rota, cnpj).status_code == 201
        assert _empresa(db_session, cnpj).cnpj_verificado_em is None
    assert receita.pedidos[0].url.path.endswith(f"/{cnpj}")


@pytest.mark.parametrize("rota", ROTAS_DE_CADASTRO)
def test_consulta_desligada_cadastra_como_antes_sem_verificar(db_session, cadastrar, rota):
    cnpj = _gerar_cnpj()

    assert _verificar(cnpj).json()["cnpj_situacao"] == "nao_verificado"
    assert cadastrar(rota, cnpj).status_code == 201
    assert _empresa(db_session, cnpj).cnpj_verificado_em is None


def test_log_do_cadastro_nao_traz_cnpj_nem_razao_social(db_session, receita, cadastrar, caplog):
    cnpj = _gerar_cnpj()
    receita.responder_ativa(cnpj)

    cadastrar("/register/google", cnpj)
    receita.resposta = httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI)
    outro = _gerar_cnpj()
    cadastrar("/register/google", outro)

    assert "consulta_cnpj resultado=ativa origem=api" in caplog.text
    assert "cadastro_google resultado=cnpj_nao_encontrado" in caplog.text
    assert cnpj not in caplog.text and outro not in caplog.text
    assert RAZAO not in caplog.text
