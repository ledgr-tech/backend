"""Cidade e UF da conta no cadastro (issue #184): `POST /register`, `POST
/register/google`, `POST /register/verificar` e `GET /me`.

As regras de texto estão em app/core/localizacao.py; a consulta de CNPJ (a
BrasilAPI num `httpx.MockTransport`, nada sai para a internet) pré-preenche só
quando o cadastro vem sem cidade e sem UF. O `/admin` está em
tests/test_admin_cidade_uf.py, a migração em tests/test_migracao_cidade_uf.py
e o vazamento nas rotas /v1 e no MCP em tests/test_api_publica.py e
tests/test_mcp_servidor.py.
"""

import logging
import uuid

import httpx
import pytest
from conftest import apagar_contas_por_cnpj
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_consulta_cnpj import corpo_brasilapi
from test_consulta_cnpj_cadastro import BASE, RAZAO, ReceitaFalsa

from app.core.localizacao import (
    CIDADE_FORA_DO_TAMANHO,
    UF_INVALIDA,
    UFS,
    cidade_da_receita,
    normalizar_cidade,
    normalizar_uf,
    uf_da_receita,
)
from app.models import Conta
from app.services.consulta_cnpj import ConsultaBrasilAPI, obter_consulta_cnpj_dependencia
from main import app

client = TestClient(app)

ROTAS = ["/register", "/register/google"]
SENHA = "senha-forte-123"
# Valores que não aparecem em nenhum outro lugar das respostas e dos logs.
CIDADE_SIGILOSA = "Cidade Sigilosa Do Teste"
CIDADE_DA_RECEITA = "ILHA DOS TESTES"
CIDADE_MUITO_LONGA = "Cidade-Sigilosa-" * 6  # 96 caracteres
AUSENTE = object()


@pytest.fixture
def receita():
    falsa = ReceitaFalsa()
    consulta = ConsultaBrasilAPI(
        base_url=BASE, cliente_http=httpx.Client(transport=httpx.MockTransport(falsa.handler))
    )
    app.dependency_overrides[obter_consulta_cnpj_dependencia] = lambda: consulta
    yield falsa
    app.dependency_overrides.pop(obter_consulta_cnpj_dependencia, None)


def _responder(receita, cnpj, municipio=CIDADE_DA_RECEITA, uf="AM", situacao="ATIVA") -> None:
    receita.resposta = httpx.Response(
        200,
        json=corpo_brasilapi(situacao=situacao, razao=RAZAO, cnpj=cnpj, municipio=municipio, uf=uf),
    )


@pytest.fixture
def cadastrar(db_session, google, gerar_cnpj):
    """`cadastrar(rota, cidade=..., uf=...)` com e-mail e CNPJ únicos; os campos
    não passados nem vão no corpo. Devolve `(resposta, cnpj)`. No fim, apaga só
    o que esses cadastros criaram."""
    cnpjs: list[str] = []

    def _cadastrar(rota: str, cidade=AUSENTE, uf=AUSENTE, cnpj: str | None = None):
        cnpj = cnpj or gerar_cnpj()
        cnpjs.append(cnpj)
        email = f"{uuid.uuid4().hex[:12]}@teste.com"
        if rota == "/register":
            corpo = {
                "nome": "Maria",
                "email": email,
                "senha": SENHA,
                "razao_social": "Empresa digitada",
                "cnpj": cnpj,
            }
        else:
            corpo = {
                "id_token": google.id_token(email=email),
                "razao_social": "Empresa digitada",
                "cnpj": cnpj,
            }
        if cidade is not AUSENTE:
            corpo["cidade"] = cidade
        if uf is not AUSENTE:
            corpo["uf"] = uf
        return client.post(rota, json=corpo), cnpj

    yield _cadastrar

    db_session.rollback()
    apagar_contas_por_cnpj(db_session, cnpjs)
    db_session.commit()


def _conta(db_session, cnpj: str) -> Conta | None:
    db_session.expire_all()
    return db_session.scalar(select(Conta).where(Conta.cnpj == cnpj))


def _local(db_session, cnpj: str) -> tuple[str | None, str | None]:
    conta = _conta(db_session, cnpj)
    return conta.cidade, conta.uf


# --- as regras de texto -----------------------------------------------------------------


def test_uf_aparada_e_em_maiuscula_e_so_as_27_siglas():
    assert len(UFS) == len(set(UFS)) == 27
    for uf in UFS:
        assert normalizar_uf(uf.lower()) == uf
    assert normalizar_uf("  sp ") == "SP"
    assert normalizar_uf("") is None
    assert normalizar_uf("  \t ") is None
    for invalida in ("XX", "S", "SPP", "1A", "São Paulo", "S P"):
        with pytest.raises(ValueError, match=UF_INVALIDA):
            normalizar_uf(invalida)


def test_cidade_aparada_sem_mudar_a_caixa_de_2_a_80_caracteres():
    assert normalizar_cidade("  ribeirão PRETO  ") == "ribeirão PRETO"
    assert normalizar_cidade("Ij") == "Ij"
    assert normalizar_cidade("a" * 80) == "a" * 80
    assert normalizar_cidade("") is None
    assert normalizar_cidade("   ") is None
    for fora in ("A", " B ", "a" * 81):
        with pytest.raises(ValueError, match=CIDADE_FORA_DO_TAMANHO):
            normalizar_cidade(fora)


def test_o_que_a_receita_traz_e_aproveitado_ou_ignorado_sem_levantar():
    assert cidade_da_receita(" SÃO PAULO ") == "SÃO PAULO"
    assert uf_da_receita("sp") == "SP"
    for estranho in (None, 123, "", "  ", "X", "a" * 81):
        assert cidade_da_receita(estranho) is None
    for estranho in (None, 123, "", "XX", "SPP"):
        assert uf_da_receita(estranho) is None


# --- cadastro, com a consulta desligada (o padrão dos testes) ----------------------------


@pytest.mark.parametrize("rota", ROTAS)
def test_sem_os_campos_a_conta_fica_sem_cidade_e_uf(db_session, cadastrar, rota):
    resposta, cnpj = cadastrar(rota)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, None)
    assert "cidade" not in resposta.json() and "uf" not in resposta.json()


@pytest.mark.parametrize("rota", ROTAS)
def test_com_cidade_e_uf_grava_aparando_e_pondo_a_uf_em_maiuscula(db_session, cadastrar, rota):
    resposta, cnpj = cadastrar(rota, cidade="  ribeirão PRETO ", uf=" sp")

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("ribeirão PRETO", "SP")


@pytest.mark.parametrize("rota", ROTAS)
def test_so_a_cidade(db_session, cadastrar, rota):
    resposta, cnpj = cadastrar(rota, cidade="Curitiba")

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Curitiba", None)


@pytest.mark.parametrize("rota", ROTAS)
def test_so_a_uf(db_session, cadastrar, rota):
    resposta, cnpj = cadastrar(rota, uf="pr")

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, "PR")


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("vazio", ["", "   ", None], ids=["vazio", "so_espacos", "nulo"])
def test_vazio_e_so_espacos_viram_nulo_em_cidade_e_em_uf(db_session, cadastrar, rota, vazio):
    resposta, cnpj = cadastrar(rota, cidade=vazio, uf=vazio)
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, None)

    resposta, cnpj = cadastrar(rota, cidade=vazio, uf="MG")
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, "MG")

    resposta, cnpj = cadastrar(rota, cidade="Niterói", uf=vazio)
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Niterói", None)


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("uf", ["XX", "S", "SPP", "1A", "São Paulo", "S P"])
def test_uf_invalida_e_422_e_nao_cria_a_conta(db_session, cadastrar, rota, uf):
    resposta, cnpj = cadastrar(rota, cidade="Curitiba", uf=uf)

    assert resposta.status_code == 422
    (erro,) = resposta.json()["detail"]
    assert erro["loc"] == ["body", "uf"] and "input" not in erro
    assert _conta(db_session, cnpj) is None


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("cidade", ["A", " B ", "a" * 81, CIDADE_MUITO_LONGA])
def test_cidade_curta_ou_longa_demais_e_422_e_nao_cria_a_conta(db_session, cadastrar, rota, cidade):
    resposta, cnpj = cadastrar(rota, cidade=cidade, uf="SP")

    assert resposta.status_code == 422
    (erro,) = resposta.json()["detail"]
    assert erro["loc"] == ["body", "cidade"] and "input" not in erro
    assert _conta(db_session, cnpj) is None


@pytest.mark.parametrize("rota", ROTAS)
def test_cidade_de_2_e_de_80_caracteres_passam(db_session, cadastrar, rota):
    resposta, cnpj = cadastrar(rota, cidade="Ij")
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Ij", None)

    resposta, cnpj = cadastrar(rota, cidade=f"  {'a' * 80}  ")
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("a" * 80, None)


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("valor", [123, True, ["SP"], {"uf": "SP"}])
def test_cidade_e_uf_que_nao_sao_texto_sao_422(db_session, cadastrar, rota, valor):
    for campo in ("cidade", "uf"):
        resposta, cnpj = cadastrar(rota, **{campo: valor})

        assert resposta.status_code == 422
        (erro,) = resposta.json()["detail"]
        assert erro["loc"] == ["body", campo] and "input" not in erro
        assert _conta(db_session, cnpj) is None


# --- cadastro, com a consulta de CNPJ ligada ---------------------------------------------


@pytest.mark.parametrize("rota", ROTAS)
def test_sem_cidade_e_sem_uf_vale_o_da_consulta(db_session, receita, cadastrar, gerar_cnpj, rota):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (CIDADE_DA_RECEITA, "AM")
    assert len(receita.pedidos) == 1


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("vazio", ["", "   ", None], ids=["vazio", "so_espacos", "nulo"])
def test_vazio_conta_como_vazio_e_a_consulta_preenche(
    db_session, receita, cadastrar, gerar_cnpj, rota, vazio
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, cidade=vazio, uf=vazio, cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (CIDADE_DA_RECEITA, "AM")


@pytest.mark.parametrize("rota", ROTAS)
def test_o_valor_digitado_vale_sobre_o_da_consulta(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, cidade="Porto Alegre", uf="rs", cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Porto Alegre", "RS")


@pytest.mark.parametrize("rota", ROTAS)
def test_so_a_cidade_digitada_a_consulta_nao_completa_a_uf(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, cidade="Porto Alegre", cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Porto Alegre", None)


@pytest.mark.parametrize("rota", ROTAS)
def test_so_a_uf_digitada_a_consulta_nao_completa_a_cidade(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, uf="rs", cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, "RS")


@pytest.mark.parametrize("rota", ROTAS)
def test_cidade_em_branco_com_uf_digitada_nao_vai_buscar_a_cidade_na_consulta(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta, _ = cadastrar(rota, cidade="   ", uf="RS", cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, "RS")


@pytest.mark.parametrize("rota", ROTAS)
def test_consulta_que_traz_so_um_dos_dois_grava_o_que_veio(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    sem_uf, sem_cidade = gerar_cnpj(), gerar_cnpj()
    _responder(receita, sem_uf, uf=None)
    resposta, _ = cadastrar(rota, cnpj=sem_uf)
    assert resposta.status_code == 201
    assert _local(db_session, sem_uf) == (CIDADE_DA_RECEITA, None)

    _responder(receita, sem_cidade, municipio=None)
    resposta, _ = cadastrar(rota, cnpj=sem_cidade)
    assert resposta.status_code == 201
    assert _local(db_session, sem_cidade) == (None, "AM")


@pytest.mark.parametrize("rota", ROTAS)
def test_dado_estranho_da_consulta_nao_derruba_o_cadastro_nem_e_gravado(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj, municipio="X", uf="ZZ")

    resposta, _ = cadastrar(rota, cnpj=cnpj)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, None)
    assert _conta(db_session, cnpj).cnpj_verificado_em is not None


@pytest.mark.parametrize("rota", ROTAS)
def test_consulta_indisponivel_deixa_cidade_e_uf_nulas_ou_as_digitadas(
    db_session, receita, cadastrar, gerar_cnpj, rota
):
    receita.resposta = httpx.Response(503, json={"type": "service_unavailable"})

    resposta, cnpj = cadastrar(rota)
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, None)

    resposta, cnpj = cadastrar(rota, cidade="Recife", uf="pe")
    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == ("Recife", "PE")
    assert len(receita.pedidos) == 2  # a mesma consulta que o cadastro já fazia


@pytest.mark.parametrize("rota", ROTAS)
def test_consulta_desligada_deixa_cidade_e_uf_nulas(db_session, cadastrar, rota):
    # Sem a fixture `receita`: a consulta está desligada, o padrão dos testes.
    resposta, cnpj = cadastrar(rota)

    assert resposta.status_code == 201
    assert _local(db_session, cnpj) == (None, None)


@pytest.mark.parametrize("rota", ROTAS)
def test_cadastro_nao_faz_chamada_externa_a_mais(db_session, receita, cadastrar, gerar_cnpj, rota):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    verificar = client.post(
        "/register/verificar", json={"email": f"{uuid.uuid4().hex[:12]}@teste.com", "cnpj": cnpj}
    )
    resposta, _ = cadastrar(rota, cnpj=cnpj)

    assert verificar.status_code == 200 and resposta.status_code == 201
    assert len(receita.pedidos) == 1  # a consulta do verificar vale pelo cache de 24 horas
    assert _local(db_session, cnpj) == (CIDADE_DA_RECEITA, "AM")


# --- POST /register/verificar ------------------------------------------------------------


def _verificar(cnpj: str) -> httpx.Response:
    return client.post(
        "/register/verificar", json={"email": f"{uuid.uuid4().hex[:12]}@teste.com", "cnpj": cnpj}
    )


def test_verificar_devolve_cidade_e_uf_da_consulta_junto_da_razao_social(receita, gerar_cnpj):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    resposta = _verificar(cnpj)

    assert resposta.status_code == 200
    assert resposta.json() == {
        "email_disponivel": True,
        "cnpj_disponivel": True,
        "cnpj_situacao": "ativa",
        "razao_social": RAZAO,
        "cidade": CIDADE_DA_RECEITA,
        "uf": "AM",
    }


def test_verificar_de_cnpj_inativo_tambem_devolve_cidade_e_uf(receita, gerar_cnpj):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj, situacao="BAIXADA")

    corpo = _verificar(cnpj).json()

    assert (corpo["cnpj_situacao"], corpo["cidade"], corpo["uf"]) == (
        "inativa",
        CIDADE_DA_RECEITA,
        "AM",
    )


def test_verificar_sem_dado_de_localizacao_na_consulta_devolve_nulos(receita, gerar_cnpj):
    cnpj = gerar_cnpj()
    receita.responder_ativa(cnpj)  # a BrasilAPI sem município nem UF

    corpo = _verificar(cnpj).json()

    assert (corpo["cnpj_situacao"], corpo["razao_social"]) == ("ativa", RAZAO)
    assert (corpo["cidade"], corpo["uf"]) == (None, None)


def test_verificar_so_com_um_dos_dois_devolve_o_que_veio(receita, gerar_cnpj):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj, uf=None)

    corpo = _verificar(cnpj).json()

    assert (corpo["cidade"], corpo["uf"]) == (CIDADE_DA_RECEITA, None)


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(503, json={"type": "service_unavailable"}),
        httpx.Response(429, json={}),
        httpx.Response(200, json={"inesperado": True}),
        httpx.ReadTimeout,
    ],
    ids=["5xx", "429", "corpo_invalido", "timeout"],
)
def test_verificar_com_consulta_indisponivel_devolve_nulos(receita, gerar_cnpj, resposta):
    receita.resposta = resposta

    corpo = _verificar(gerar_cnpj()).json()

    assert corpo["cnpj_situacao"] == "nao_verificado"
    assert (corpo["razao_social"], corpo["cidade"], corpo["uf"]) == (None, None, None)


def test_verificar_com_consulta_desligada_devolve_nulos(gerar_cnpj):
    corpo = _verificar(gerar_cnpj()).json()

    assert corpo["cnpj_situacao"] == "nao_verificado"
    assert (corpo["razao_social"], corpo["cidade"], corpo["uf"]) == (None, None, None)


def test_verificar_de_cnpj_que_ja_tem_conta_nao_devolve_localizacao(
    db_session, receita, criar_empresa
):
    empresa = criar_empresa()
    _responder(receita, empresa.cnpj)

    corpo = _verificar(empresa.cnpj).json()

    assert corpo["cnpj_disponivel"] is False
    assert (corpo["cidade"], corpo["uf"]) == (None, None)
    assert receita.pedidos == []


# --- GET /me -----------------------------------------------------------------------------


def test_me_devolve_cidade_e_uf_da_conta(db_session, criar_usuario, gerar_token):
    usuario = criar_usuario()
    conta = db_session.get(Conta, usuario.conta_id)
    conta.cidade, conta.uf = "Ribeirão Preto", "SP"
    db_session.commit()
    token = gerar_token(usuario.conta_id, usuario_id=usuario.id)

    corpo = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert (corpo["cidade"], corpo["uf"]) == ("Ribeirão Preto", "SP")


def test_me_de_conta_antiga_devolve_nulos(db_session, criar_usuario, gerar_token):
    usuario = criar_usuario()  # sem cidade nem UF, como as contas de antes da #184
    token = gerar_token(usuario.conta_id, usuario_id=usuario.id)

    resposta = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert "cidade" in corpo and "uf" in corpo
    assert (corpo["cidade"], corpo["uf"]) == (None, None)


def test_me_de_conta_recem_cadastrada_traz_o_que_foi_gravado(
    db_session, cadastrar, gerar_token, receita, gerar_cnpj
):
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)
    resposta, _ = cadastrar("/register", cidade="Manaus", cnpj=cnpj)
    assert resposta.status_code == 201
    token = gerar_token(uuid.UUID(resposta.json()["conta_id"]), usuario_id=resposta.json()["id"])

    corpo = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert (corpo["cidade"], corpo["uf"]) == ("Manaus", None)


# --- vazamento ---------------------------------------------------------------------------


@pytest.mark.parametrize("rota", ROTAS)
def test_422_nao_repete_o_valor_de_cidade_nem_de_uf(db_session, cadastrar, rota):
    resposta, _ = cadastrar(rota, cidade=CIDADE_MUITO_LONGA, uf="Zz9")

    assert resposta.status_code == 422
    erros = resposta.json()["detail"]
    assert {tuple(erro["loc"]) for erro in erros} == {("body", "cidade"), ("body", "uf")}
    for erro in erros:
        assert "input" not in erro
    assert "Cidade-Sigilosa" not in resposta.text and "Zz9" not in resposta.text
    # A mensagem diz o que falta, sem o valor.
    mensagens = " ".join(erro["msg"] for erro in erros)
    assert CIDADE_FORA_DO_TAMANHO in mensagens and UF_INVALIDA in mensagens


def test_422_de_outro_campo_continua_com_o_input_do_fastapi(db_session, gerar_cnpj):
    corpo = {
        "nome": "Maria",
        "email": "isto-nao-e-email",
        "senha": SENHA,
        "razao_social": "Empresa",
        "cnpj": gerar_cnpj(),
        "cidade": CIDADE_SIGILOSA,
        "uf": "SP",
    }

    resposta = client.post("/register", json=corpo)

    assert resposta.status_code == 422
    (erro,) = resposta.json()["detail"]
    assert erro["loc"] == ["body", "email"] and erro["input"] == "isto-nao-e-email"
    assert CIDADE_SIGILOSA not in resposta.text


def test_422_do_corpo_inteiro_tambem_nao_devolve_o_que_foi_enviado(db_session):
    resposta = client.post("/register", json=[{"cidade": CIDADE_SIGILOSA, "uf": "SP"}])

    assert resposta.status_code == 422
    assert CIDADE_SIGILOSA not in resposta.text
    assert all("input" not in erro for erro in resposta.json()["detail"])


@pytest.mark.parametrize("rota", ROTAS)
def test_nada_vai_para_o_log_no_cadastro_com_sucesso_ou_422(
    db_session, receita, cadastrar, gerar_cnpj, caplog, rota
):
    caplog.set_level(logging.DEBUG)
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)

    sucesso, _ = cadastrar(rota, cidade=CIDADE_SIGILOSA, uf="ro", cnpj=cnpj)
    pela_consulta, _ = cadastrar(rota, cnpj=_novo_cnpj_com_consulta(receita, gerar_cnpj))
    recusado, _ = cadastrar(rota, cidade=CIDADE_MUITO_LONGA, uf="Zz9")

    assert (sucesso.status_code, pela_consulta.status_code, recusado.status_code) == (201, 201, 422)
    for texto in (CIDADE_SIGILOSA, "Cidade-Sigilosa", CIDADE_DA_RECEITA, "Zz9", "'cidade'"):
        assert texto not in caplog.text, texto


def _novo_cnpj_com_consulta(receita, gerar_cnpj) -> str:
    cnpj = gerar_cnpj()
    _responder(receita, cnpj)
    return cnpj


def test_nada_vai_para_o_log_no_verificar_nem_no_me(
    db_session, receita, gerar_cnpj, criar_usuario, gerar_token, caplog
):
    caplog.set_level(logging.DEBUG)
    cnpj = gerar_cnpj()
    _responder(receita, cnpj, municipio=CIDADE_DA_RECEITA, uf="AM")
    usuario = criar_usuario()
    conta = db_session.get(Conta, usuario.conta_id)
    conta.cidade, conta.uf = CIDADE_SIGILOSA, "RO"
    db_session.commit()
    token = gerar_token(usuario.conta_id, usuario_id=usuario.id)

    verificar = _verificar(cnpj)
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert verificar.status_code == me.status_code == 200
    assert me.json()["cidade"] == CIDADE_SIGILOSA
    for texto in (CIDADE_SIGILOSA, CIDADE_DA_RECEITA, "'RO'", "uf=", "cidade="):
        assert texto not in caplog.text, texto
