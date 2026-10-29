"""API pública e chaves de API (issue #135, ADR-021).

As chaves são criadas e revogadas pelo app (`/chaves-api`, JWT, admin ou
gestor) e autenticam só as rotas `/v1`, que são de leitura e nunca devolvem
descrição de lançamento, texto de justificativa, texto da ressalva nem nome de
pessoa. Nenhum teste fala com a internet.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.api import chaves_api
from app.core import auth
from app.core.config import settings
from app.core.database import engine
from app.models import ChaveApi, Fechamento
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
NOME_DA_PESSOA = "Joana Pessoa Secreta"
DESCRICAO_SECRETA = "PAGAMENTO FORNECEDOR SIGILOSO"
JUSTIFICATIVA_SECRETA = "Texto confidencial da justificativa"
RESSALVA_SECRETA = "Ressalva confidencial do fechamento"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def conta(
    db_session,
    criar_usuario,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
):
    """Conta com admin (token do app), empresas e fábricas de chave e de par
    conciliado pela API do app."""

    def _nova():
        admin = criar_usuario(nome=NOME_DA_PESSOA)
        empresa_id = empresa_unica(db_session, admin.conta_id)
        headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id), empresa_id)

        def com_papel(papel):
            usuario = criar_usuario(conta_id=admin.conta_id, papel=papel, nome=NOME_DA_PESSOA)
            return cabecalhos(gerar_token(admin.conta_id, usuario_id=usuario.id))

        def outra_empresa(razao_social="Outra Empresa Ltda"):
            return criar_empresa(razao_social=razao_social, conta_id=admin.conta_id).id

        def criar_chave(nome="Integração", empresa=None, esperado=201, cabecalho=None):
            corpo = {"nome": nome}
            if empresa is not None:
                corpo["empresa_id"] = str(empresa)
            resposta = client.post("/chaves-api", headers=cabecalho or headers, json=corpo)
            assert resposta.status_code == esperado, resposta.text
            return resposta.json()

        def extrato(origem, itens, empresa=None):
            novo = criar_extrato_da_empresa(empresa or empresa_id, origem)
            inserir_lancamentos(novo, list(itens))
            novo.periodo_inicio, novo.periodo_fim = SETEMBRO
            db_session.commit()
            return novo

        def par(itens_banco, itens_sistema):
            banco = extrato("banco", itens_banco)
            sistema = extrato("sistema", itens_sistema)
            resposta = client.post(
                "/conciliacoes",
                headers=headers,
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == 201, resposta.text
            return banco, sistema

        def itens_do_app(banco):
            return client.get(f"/conciliacoes/{banco.id}", headers=headers).json()["itens"]

        def decidir(banco, valor, tipo, texto=None):
            chave = next(
                item["chave"]
                for item in itens_do_app(banco)
                if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == valor
            )
            resposta = client.post(
                f"/conciliacoes/{banco.id}/decisoes",
                headers=headers,
                json={"chave": chave, "tipo": tipo, "texto": texto},
            )
            assert resposta.status_code == 200, resposta.text

        def fechar(ressalva=None):
            corpo = {"competencia": "2026-09"}
            if ressalva is not None:
                corpo["ressalva"] = ressalva
            resposta = client.post("/fechamentos", headers=headers, json=corpo)
            assert resposta.status_code == 201, resposta.text

        return SimpleNamespace(
            admin=admin,
            conta_id=admin.conta_id,
            empresa_id=empresa_id,
            headers=headers,
            com_papel=com_papel,
            outra_empresa=outra_empresa,
            criar_chave=criar_chave,
            extrato=extrato,
            par=par,
            itens_do_app=itens_do_app,
            decidir=decidir,
            fechar=fechar,
        )

    return _nova


def _v1(token, caminho, **params):
    return client.get(f"/v1{caminho}", headers=_bearer(token), params=params)


# --- chaves pelo app ------------------------------------------------------------------


def test_criar_chave_mostra_o_token_uma_vez_e_grava_so_o_hash(conta, db_session):
    c = conta()

    criada = c.criar_chave(nome="  Power BI  ")

    token = criada["chave"]
    assert token.startswith("ledgr_") and len(token) > 40
    assert criada["nome"] == "Power BI"
    assert criada["prefixo"] == token[6:14]
    assert criada["escopo"] == {"tipo": "conta"}
    assert criada["criada_por"] == NOME_DA_PESSOA
    assert criada["ultimo_uso_em"] is None and criada["revogada_em"] is None
    linha = db_session.get(ChaveApi, uuid.UUID(criada["id"]))
    assert linha.hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in {linha.nome, linha.prefixo, linha.hash}

    (listada,) = client.get("/chaves-api", headers=c.headers).json()["itens"]
    assert "chave" not in listada and "hash" not in listada
    assert {k: v for k, v in criada.items() if k != "chave"} == listada


def test_lista_ativas_e_revogadas_das_mais_novas_com_escopo(conta):
    c = conta()
    da_conta = c.criar_chave(nome="Conta")
    da_empresa = c.criar_chave(nome="Empresa", empresa=c.empresa_id)
    assert client.delete(f"/chaves-api/{da_conta['id']}", headers=c.headers).status_code == 204

    itens = client.get("/chaves-api", headers=c.headers).json()["itens"]

    assert [i["id"] for i in itens] == [da_empresa["id"], da_conta["id"]]
    assert itens[0]["escopo"] == {
        "tipo": "empresa",
        "empresa_id": str(c.empresa_id),
        "razao_social": da_empresa["escopo"]["razao_social"],
    }
    assert itens[0]["revogada_em"] is None
    assert itens[1]["revogada_em"] is not None


def test_empresa_de_outra_conta_ou_inexistente_na_criacao_da_404(conta):
    c, outra = conta(), conta()

    for empresa in (outra.empresa_id, uuid.uuid4()):
        resposta = client.post(
            "/chaves-api", headers=c.headers, json={"nome": "x", "empresa_id": str(empresa)}
        )
        assert resposta.status_code == 404
        assert resposta.json() == {"detail": "Empresa não encontrada."}


@pytest.mark.parametrize("nome", ["", "   ", "x" * 61])
def test_nome_invalido_da_422(conta, nome):
    c = conta()

    resposta = client.post("/chaves-api", headers=c.headers, json={"nome": nome})

    assert resposta.status_code == 422


def test_gestor_gerencia_e_analista_recebe_403(conta):
    c = conta()
    gestor, analista = c.com_papel("gestor"), c.com_papel("analista")
    criada = c.criar_chave(cabecalho=gestor)

    mensagem = {"detail": "Só o admin ou o gestor da conta podem fazer isso."}
    for resposta in (
        client.get("/chaves-api", headers=analista),
        client.post("/chaves-api", headers=analista, json={"nome": "x"}),
        client.delete(f"/chaves-api/{criada['id']}", headers=analista),
    ):
        assert resposta.status_code == 403 and resposta.json() == mensagem
    assert len(client.get("/chaves-api", headers=gestor).json()["itens"]) == 1


def test_limite_de_10_chaves_ativas(conta, monkeypatch):
    c = conta()
    criadas = [c.criar_chave(nome=f"Chave {i}") for i in range(10)]

    resposta = client.post("/chaves-api", headers=c.headers, json={"nome": "Mais uma"})

    assert resposta.status_code == 409
    assert resposta.json() == {
        "detail": "Sua conta já tem 10 chaves ativas. Revogue uma antes de criar outra."
    }
    client.delete(f"/chaves-api/{criadas[0]['id']}", headers=c.headers)
    c.criar_chave(nome="Depois de revogar")
    assert chaves_api.MAXIMO_CHAVES_ATIVAS == 10


def test_revogar_vale_na_hora_e_e_idempotente(conta, db_session):
    c = conta()
    criada = c.criar_chave()
    assert _v1(criada["chave"], "/empresas").status_code == 200

    primeira = client.delete(f"/chaves-api/{criada['id']}", headers=c.headers)
    db_session.expire_all()
    revogada_em = db_session.get(ChaveApi, uuid.UUID(criada["id"])).revogada_em
    segunda = client.delete(f"/chaves-api/{criada['id']}", headers=c.headers)
    db_session.expire_all()
    linha = db_session.get(ChaveApi, uuid.UUID(criada["id"]))

    assert primeira.status_code == 204 and segunda.status_code == 204
    assert revogada_em is not None and linha.revogada_em == revogada_em
    assert linha.revogada_por_id == c.admin.id
    resposta = _v1(criada["chave"], "/empresas")
    assert resposta.status_code == 401
    assert resposta.json() == {"detail": "Chave de API inválida."}


def test_revogar_chave_de_outra_conta_ou_inexistente_da_404(conta):
    c, outra = conta(), conta()
    alheia = outra.criar_chave()

    for chave_id in (alheia["id"], uuid.uuid4()):
        assert client.delete(f"/chaves-api/{chave_id}", headers=c.headers).status_code == 404
    assert _v1(alheia["chave"], "/empresas").status_code == 200


def test_chaves_sao_da_conta_e_ignoram_a_empresa_ativa(conta):
    c, outra = conta(), conta()
    c.criar_chave()
    outra.criar_chave()

    sem_empresa = client.get("/chaves-api", headers={"Authorization": c.headers["Authorization"]})

    assert sem_empresa.status_code == 200 and len(sem_empresa.json()["itens"]) == 1


# --- autenticação ---------------------------------------------------------------------


def test_sem_chave_chave_desconhecida_ou_jwt_do_app_nas_v1_dao_o_mesmo_401(conta):
    c = conta()
    c.criar_chave()
    token_do_app = c.headers["Authorization"].removeprefix("Bearer ")

    for cabecalho in (
        {},
        _bearer(token_do_app),
        _bearer("ledgr_" + "x" * 43),
        _bearer("ledgr_"),
        {"Authorization": "Basic abc"},
    ):
        resposta = client.get("/v1/empresas", headers=cabecalho)
        assert resposta.status_code == 401, cabecalho
        assert resposta.json() == {"detail": "Chave de API inválida."}
        assert resposta.headers["www-authenticate"] == "Bearer"


def test_chave_nas_rotas_do_app_da_401(conta):
    c = conta()
    token = c.criar_chave()["chave"]

    for caminho in ("/me", "/chaves-api", "/fechamentos"):
        resposta = client.get(
            caminho, headers=_bearer(token) | {"X-Ledgr-Empresa": str(c.empresa_id)}
        )
        assert resposta.status_code == 401, caminho


def test_rate_limit_por_chave_com_retry_after(conta):
    c = conta()
    primeira, segunda = c.criar_chave()["chave"], c.criar_chave()["chave"]

    status = [_v1(primeira, "/empresas").status_code for _ in range(60)]
    estourou = _v1(primeira, "/empresas")

    assert set(status) == {200}
    assert estourou.status_code == 429
    assert 1 <= int(estourou.headers["retry-after"]) <= 60
    assert isinstance(estourou.json()["detail"], str)
    assert _v1(segunda, "/empresas").status_code == 200


def test_ultimo_uso_gravado_no_maximo_uma_vez_por_minuto(conta, db_session, monkeypatch):
    c = conta()
    criada = c.criar_chave()
    chave_id = uuid.UUID(criada["id"])
    chamadas = []
    original = auth.registrar_uso_da_chave
    monkeypatch.setattr(auth, "registrar_uso_da_chave", lambda i: (chamadas.append(i), original(i)))

    for _ in range(3):
        assert _v1(criada["chave"], "/empresas").status_code == 200
    db_session.expire_all()
    primeiro_uso = db_session.get(ChaveApi, chave_id).ultimo_uso_em

    assert chamadas == [chave_id]
    assert primeiro_uso is not None

    antigo = datetime.now(UTC) - timedelta(minutes=2)
    db_session.get(ChaveApi, chave_id).ultimo_uso_em = antigo
    db_session.commit()
    _v1(criada["chave"], "/empresas")
    db_session.expire_all()

    assert len(chamadas) == 2
    assert db_session.get(ChaveApi, chave_id).ultimo_uso_em > antigo
    listada = client.get("/chaves-api", headers=c.headers).json()["itens"][0]
    assert listada["ultimo_uso_em"] is not None


def test_log_tem_id_e_prefixo_da_chave_e_nunca_o_token(conta, caplog):
    c = conta()
    criada = c.criar_chave()

    caplog.clear()
    _v1(criada["chave"], f"/empresas/{c.empresa_id}/resumo", competencia="2026-09")

    texto = caplog.text
    assert f"chave_id={criada['id']}" in texto
    assert f"prefixo={criada['prefixo']}" in texto
    assert "rota=/v1/empresas/{empresa_id}/resumo" in texto and "status=200" in texto
    assert criada["chave"] not in texto
    assert criada["chave"][6:] not in texto


# --- escopo ---------------------------------------------------------------------------


def test_chave_da_conta_ve_todas_as_empresas_da_conta_e_nenhuma_de_outra(conta):
    c, outra = conta(), conta()
    segunda = c.outra_empresa()
    token = c.criar_chave()["chave"]

    empresas = _v1(token, "/empresas").json()["itens"]

    assert {e["id"] for e in empresas} == {str(c.empresa_id), str(segunda)}
    assert set(empresas[0]) == {"id", "razao_social", "cnpj"}
    for empresa in (c.empresa_id, segunda):
        assert _v1(token, f"/empresas/{empresa}/resumo", competencia="2026-09").status_code == 200
    alheia = _v1(token, f"/empresas/{outra.empresa_id}/resumo", competencia="2026-09")
    assert alheia.status_code == 404 and alheia.json() == {"detail": "Empresa não encontrada."}


def test_chave_de_uma_empresa_nao_le_outra(conta):
    c = conta()
    segunda = c.outra_empresa()
    token = c.criar_chave(empresa=c.empresa_id)["chave"]

    assert [e["id"] for e in _v1(token, "/empresas").json()["itens"]] == [str(c.empresa_id)]
    for caminho in ("resumo", "conciliacoes", "divergencias", "fechamentos"):
        for empresa in (segunda, uuid.uuid4()):
            resposta = _v1(token, f"/empresas/{empresa}/{caminho}", competencia="2026-09")
            assert resposta.status_code == 404, caminho
            assert resposta.json() == {"detail": "Empresa não encontrada."}
        assert (
            _v1(token, f"/empresas/{c.empresa_id}/{caminho}", competencia="2026-09").status_code
            == 200
        )


@pytest.mark.parametrize("competencia", ["2026-13", "2026-9", "setembro", "2026-00"])
@pytest.mark.parametrize("caminho", ["resumo", "conciliacoes", "divergencias", "fechamentos"])
def test_competencia_invalida_da_422(conta, competencia, caminho):
    c = conta()
    token = c.criar_chave()["chave"]

    resposta = _v1(token, f"/empresas/{c.empresa_id}/{caminho}", competencia=competencia)

    assert resposta.status_code == 422


def test_resumo_e_divergencias_exigem_competencia(conta):
    c = conta()
    token = c.criar_chave()["chave"]

    for caminho in ("resumo", "conciliacoes", "divergencias"):
        assert _v1(token, f"/empresas/{c.empresa_id}/{caminho}").status_code == 422


# --- conteúdo -------------------------------------------------------------------------


def _mes_com_tudo(c):
    """Um par com match, divergências justificada, conferida e pendente, e o
    mês fechado com ressalva."""
    banco, sistema = c.par(
        [("10.00", "a"), ("20.00", DESCRICAO_SECRETA), ("-5.50", "c"), ("33.30", "d")],
        [("10.00", "a"), ("7.00", DESCRICAO_SECRETA)],
    )
    c.decidir(banco, "20.00", "justificada", JUSTIFICATIVA_SECRETA)
    c.decidir(banco, "-5.50", "conferida")
    c.fechar(ressalva=RESSALVA_SECRETA)
    return banco, sistema


def test_respostas_da_v1_nao_tem_descricao_justificativa_ressalva_nem_nomes(conta):
    c = conta()
    _mes_com_tudo(c)
    token = c.criar_chave()["chave"]

    respostas = [
        _v1(token, "/empresas"),
        _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-09"),
        _v1(token, f"/empresas/{c.empresa_id}/conciliacoes", competencia="2026-09"),
        _v1(token, f"/empresas/{c.empresa_id}/divergencias", competencia="2026-09", limit=200),
        _v1(token, f"/empresas/{c.empresa_id}/fechamentos"),
    ]

    for resposta in respostas:
        assert resposta.status_code == 200, resposta.text
        for proibido in (
            DESCRICAO_SECRETA,
            JUSTIFICATIVA_SECRETA,
            RESSALVA_SECRETA,
            NOME_DA_PESSOA,
            "extrato.csv",
        ):
            assert proibido not in resposta.text, (resposta.url, proibido)
    assert respostas[3].json()["total"] > 0


def test_resumo_conciliacoes_e_fechamentos_do_mes(conta):
    c = conta()
    banco, sistema = _mes_com_tudo(c)
    token = c.criar_chave()["chave"]

    resumo = _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-09").json()
    (conciliacao,) = _v1(
        token, f"/empresas/{c.empresa_id}/conciliacoes", competencia="2026-09"
    ).json()["itens"]
    (fechamento,) = _v1(token, f"/empresas/{c.empresa_id}/fechamentos").json()["itens"]

    assert resumo["competencia"] == "2026-09"
    assert resumo["fechado"] is True and resumo["com_ressalva"] is True
    assert datetime.fromisoformat(resumo["fechado_em"]).tzinfo is not None
    assert resumo["pronto_para_fechar"] is False
    contagens = resumo["contagens"]
    assert contagens["total"] == 5 and contagens["match_exato"] == 1
    assert contagens["justificadas"] == 1 and contagens["aguardando_aprovacao"] == 0
    assert contagens["linhas_nao_lidas"] == 0
    assert set(contagens) == {
        "total",
        "match_exato",
        "match_tolerancia",
        "duplicado",
        "sem_correspondencia",
        "tarifa_bancaria",
        "divergente_valor",
        "divergente_data",
        "justificadas",
        "aguardando_aprovacao",
        "pendentes",
        "linhas_nao_lidas",
    }

    assert conciliacao["extrato_banco_id"] == str(banco.id)
    assert conciliacao["extrato_sistema_id"] == str(sistema.id)
    assert conciliacao["rodada"] == 1
    assert datetime.fromisoformat(conciliacao["executada_em"]).tzinfo is not None
    assert (
        conciliacao["periodo_inicio"] == "2026-09-01" and conciliacao["periodo_fim"] == "2026-09-30"
    )
    assert conciliacao["contagens"]["total"] == 5 and conciliacao["contagens"]["justificadas"] == 1

    assert fechamento["competencia"] == "2026-09" and fechamento["com_ressalva"] is True
    assert fechamento["reaberto_em"] is None
    assert fechamento["contagens"] == contagens


def test_mes_sem_conciliacao_nao_esta_pronto(conta):
    c = conta()
    token = c.criar_chave()["chave"]

    resumo = _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-10").json()

    assert resumo["fechado"] is False and resumo["fechado_em"] is None
    assert resumo["com_ressalva"] is False and resumo["pronto_para_fechar"] is False
    assert resumo["contagens"]["total"] == 0
    assert (
        _v1(token, f"/empresas/{c.empresa_id}/conciliacoes", competencia="2026-10").json()["itens"]
        == []
    )


def test_mes_justificado_aberto_esta_pronto(conta):
    c = conta()
    banco, _ = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    c.decidir(banco, "20.00", "justificada", "motivo")
    token = c.criar_chave()["chave"]

    resumo = _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-09").json()

    assert resumo["fechado"] is False and resumo["pronto_para_fechar"] is True


def test_divergencias_iguais_as_do_app_com_situacao_lado_e_valor(conta):
    c = conta()
    banco, _ = _mes_com_tudo(c)
    token = c.criar_chave()["chave"]

    corpo = _v1(token, f"/empresas/{c.empresa_id}/divergencias", competencia="2026-09").json()

    do_app = {
        (i["chave"], i["status"])
        for i in c.itens_do_app(banco)
        if i["status"] not in ("match_exato", "match_tolerancia")
    }
    assert {(d["chave"], d["categoria"]) for d in corpo["itens"]} == do_app
    assert corpo["total"] == len(do_app) == 4
    assert corpo["limit"] == 50 and corpo["offset"] == 0
    por_valor = {d["valor"]: d for d in corpo["itens"]}
    assert por_valor["20.00"]["situacao"] == "justificada"
    assert por_valor["-5.50"]["situacao"] == "conferida"
    assert por_valor["33.30"]["situacao"] == "sem_justificativa"
    assert por_valor["7.00"]["lado"] == "sistema"
    assert all(d["data"] == "2026-09-05" for d in corpo["itens"])
    assert all(d["extrato_banco_id"] == str(banco.id) for d in corpo["itens"])
    assert set(corpo["itens"][0]) == {
        "extrato_banco_id",
        "chave",
        "categoria",
        "data",
        "valor",
        "lado",
        "situacao",
    }


def test_divergencias_filtros_e_paginacao(conta):
    c = conta()
    _mes_com_tudo(c)
    token = c.criar_chave()["chave"]
    caminho = f"/empresas/{c.empresa_id}/divergencias"

    todas = _v1(token, caminho, competencia="2026-09").json()["itens"]
    pagina = _v1(token, caminho, competencia="2026-09", limit=2, offset=1).json()
    pendentes = _v1(token, caminho, competencia="2026-09", situacao="sem_justificativa").json()
    categoria = todas[0]["categoria"]
    da_categoria = _v1(token, caminho, competencia="2026-09", categoria=categoria).json()

    assert pagina["total"] == 4 and pagina["itens"] == todas[1:3]
    assert pendentes["total"] == 2
    assert {d["situacao"] for d in pendentes["itens"]} == {"sem_justificativa"}
    assert da_categoria["total"] == sum(d["categoria"] == categoria for d in todas)
    for params in (
        {"limit": 201},
        {"limit": 0},
        {"offset": -1},
        {"situacao": "x"},
        {"categoria": "match_exato"},
    ):
        assert _v1(token, caminho, competencia="2026-09", **params).status_code == 422, params


def test_justificativa_aguardando_aprovacao(conta, monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    c = conta()
    banco, _ = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    c.decidir(banco, "20.00", "justificada", "motivo")
    token = c.criar_chave()["chave"]

    resumo = _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-09").json()
    (linha,) = _v1(token, f"/empresas/{c.empresa_id}/divergencias", competencia="2026-09").json()[
        "itens"
    ]

    assert resumo["contagens"]["aguardando_aprovacao"] == 1
    assert resumo["pronto_para_fechar"] is False
    assert linha["situacao"] == "aguardando_aprovacao"


def test_fechamento_reaberto_aparece_com_reaberto_em(conta, db_session):
    c = conta()
    c.par([("10.00", "a")], [("10.00", "a")])
    c.fechar()
    assert client.delete("/fechamentos/2026-09", headers=c.headers).status_code == 200
    token = c.criar_chave()["chave"]

    (fechamento,) = _v1(
        token, f"/empresas/{c.empresa_id}/fechamentos", competencia="2026-09"
    ).json()["itens"]
    resumo = _v1(token, f"/empresas/{c.empresa_id}/resumo", competencia="2026-09").json()

    assert fechamento["reaberto_em"] is not None and fechamento["com_ressalva"] is False
    assert resumo["fechado"] is False and resumo["pronto_para_fechar"] is True
    assert db_session.query(Fechamento).filter_by(empresa_id=c.empresa_id).count() == 1
    assert _v1(token, f"/empresas/{c.empresa_id}/fechamentos", competencia="2026-08").json() == {
        "itens": []
    }


def _selects(funcao) -> int:
    selects: list[str] = []

    def contar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        resposta = funcao()
    finally:
        event.remove(engine, "before_cursor_execute", contar)
    assert resposta.status_code == 200, resposta.text
    return len(selects)


@pytest.mark.parametrize("caminho", ["resumo", "conciliacoes", "divergencias", "fechamentos"])
def test_numero_fixo_de_consultas(conta, caminho):
    c = conta()
    token = c.criar_chave()["chave"]
    c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    c.fechar(ressalva="r")
    url = f"/v1/empresas/{c.empresa_id}/{caminho}"

    def chamar():
        return client.get(url, headers=_bearer(token), params={"competencia": "2026-09"})

    pequeno = _selects(chamar)
    client.delete("/fechamentos/2026-09", headers=c.headers)
    for i in range(3):
        b, _ = c.par([(f"{i + 1}1.00", "x"), ("5.00", "y"), ("6.00", "z")], [(f"{i + 1}1.00", "x")])
        c.decidir(b, "5.00", "justificada", "motivo")
    c.fechar(ressalva="r")
    grande = _selects(chamar)

    assert grande == pequeno, (pequeno, grande)


# --- documentação pública -------------------------------------------------------------


def test_openapi_da_v1_e_publico_e_so_tem_as_rotas_v1():
    resposta = client.get("/v1/openapi.json")

    assert resposta.status_code == 200
    especificacao = resposta.json()
    assert set(especificacao["paths"]) == {
        "/v1/empresas",
        "/v1/empresas/{empresa_id}/resumo",
        "/v1/empresas/{empresa_id}/conciliacoes",
        "/v1/empresas/{empresa_id}/divergencias",
        "/v1/empresas/{empresa_id}/fechamentos",
    }
    for operacoes in especificacao["paths"].values():
        assert operacoes["get"]["description"]
    assert "chave de API" in especificacao["info"]["description"]


def test_openapi_do_app_nao_lista_a_v1():
    caminhos = client.get("/openapi.json").json()["paths"]

    assert "/chaves-api" in caminhos
    assert not [c for c in caminhos if c.startswith("/v1")]


def test_llms_txt_publico_em_markdown():
    resposta = client.get("/llms.txt")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/plain")
    texto = resposta.text
    assert texto.startswith("# Ledgr")
    assert "Authorization: Bearer ledgr_" in texto
    assert "/v1/empresas/{empresa_id}/divergencias" in texto
    assert "60 requisições por minuto" in texto


def test_sem_cors_na_v1(conta):
    c = conta()
    token = c.criar_chave()["chave"]

    resposta = client.get(
        "/v1/empresas", headers=_bearer(token) | {"Origin": "https://exemplo.com"}
    )

    assert resposta.status_code == 200
    assert "access-control-allow-origin" not in resposta.headers
