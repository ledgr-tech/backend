"""Testes do fechamento do mês (issue #86): rotas /fechamentos, regra do mês,
bloqueio de conciliar e decidir em mês fechado, e as travas.

Precisam de Postgres real (`db_session` do conftest.py) e pulam sem ele.
"""

import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import engine
from app.models import Conciliacao, DecisaoLinha, ExecucaoConciliacao, LinhaInvalida
from app.services.travas import chave_do_extrato_banco, chave_do_mes
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture
def conta(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """Usuário com token e uma fábrica de pares conciliados pela API."""

    def _nova():
        usuario = criar_usuario()
        token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
        headers = {"Authorization": f"Bearer {token}"}

        def extrato(origem, itens, periodo=SETEMBRO):
            novo = criar_extrato_da_empresa(usuario.empresa_id, origem)
            inserir_lancamentos(novo, list(itens))
            novo.periodo_inicio, novo.periodo_fim = periodo if periodo else (None, None)
            db_session.commit()
            return novo

        def conciliar(banco, sistema, esperado=201):
            resposta = client.post(
                "/conciliacoes",
                headers=headers,
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == esperado, resposta.text
            return resposta

        def par(itens_banco, itens_sistema, periodo=SETEMBRO):
            banco = extrato("banco", itens_banco, periodo)
            sistema = extrato("sistema", itens_sistema, periodo)
            conciliar(banco, sistema)
            return banco, sistema

        def decidir(banco, valor, tipo, texto=None, sistema=None):
            params = {"extrato_sistema_id": str(sistema.id)} if sistema else {}
            itens = client.get(f"/conciliacoes/{banco.id}", headers=headers, params=params).json()[
                "itens"
            ]
            chave = next(
                item["chave"]
                for item in itens
                if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == valor
            )
            return client.post(
                f"/conciliacoes/{banco.id}/decisoes",
                headers=headers,
                json={"chave": chave, "tipo": tipo, "texto": texto},
            )

        def fechar(competencia="2026-09", ressalva=None):
            corpo = {"competencia": competencia}
            if ressalva is not None:
                corpo["ressalva"] = ressalva
            return client.post("/fechamentos", headers=headers, json=corpo)

        def reabrir(competencia="2026-09"):
            return client.delete(f"/fechamentos/{competencia}", headers=headers)

        def listar(**params):
            return client.get("/fechamentos", headers=headers, params=params)

        return SimpleNamespace(
            usuario=usuario,
            headers=headers,
            extrato=extrato,
            conciliar=conciliar,
            par=par,
            decidir=decidir,
            fechar=fechar,
            reabrir=reabrir,
            listar=listar,
        )

    return _nova


def _com_fuso(valor):
    return datetime.fromisoformat(valor).tzinfo is not None


def _linha_invalida(db_session, extrato):
    db_session.add(LinhaInvalida(extrato_id=extrato.id, identificador="3", motivo="Valor inválido"))
    db_session.commit()


# Fechar


def test_fechar_mes_pronto(conta):
    c = conta()
    banco, _ = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    assert c.decidir(banco, "20.00", "justificada", "motivo").status_code == 200

    resposta = c.fechar()

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["competencia"] == "2026-09"
    assert corpo["estado"] == "fechado"
    assert corpo["ressalva"] is None
    assert corpo["fechado_por"] == c.usuario.nome
    assert _com_fuso(corpo["fechado_em"])
    assert corpo["reaberto_por"] is None and corpo["reaberto_em"] is None
    resumo = corpo["resumo"]
    assert resumo["contagens"]["total"] == 2
    assert resumo["contagens"]["match_exato"] == 1
    assert resumo["justificadas"] == 1
    assert resumo["pendentes"] == 0
    assert resumo["linhas_nao_lidas"] == 0
    assert resumo["valor_em_aberto"] == "0.00"
    (par,) = resumo["pares"]
    assert par["extrato_banco_id"] == str(banco.id) and par["rodada"] == 1


def test_divergencia_sem_justificativa_da_409_e_ressalva_permite(conta):
    c = conta()
    c.par([("10.00", "a"), ("20.00", "b"), ("-5.50", "c")], [("10.00", "a")])

    sem_ressalva = c.fechar()
    so_espacos = c.fechar(ressalva="   ")
    longa = c.fechar(ressalva="x" * 1001)
    com_ressalva = c.fechar(ressalva="  Diferença conhecida.  ")

    esperado = (
        "Há 2 divergências sem justificativa nesta competência. "
        "Justifique, corrija ou feche com ressalva."
    )
    assert sem_ressalva.status_code == 409 and sem_ressalva.json() == {"detail": esperado}
    assert so_espacos.status_code == 409
    assert longa.status_code == 422 and isinstance(longa.json()["detail"], str)
    assert com_ressalva.status_code == 201
    corpo = com_ressalva.json()
    assert corpo["ressalva"] == "Diferença conhecida."
    assert corpo["resumo"]["pendentes"] == 2
    assert corpo["resumo"]["valor_em_aberto"] == "25.50"


@pytest.mark.parametrize("lado", ["banco", "sistema"])
def test_linha_nao_lida_da_409_e_ressalva_permite(conta, db_session, lado):
    c = conta()
    banco, sistema = c.par([("10.00", "a")], [("10.00", "a")])
    _linha_invalida(db_session, banco if lado == "banco" else sistema)

    sem_ressalva = c.fechar()
    com_ressalva = c.fechar(ressalva="Linha ilegível no arquivo.")

    assert sem_ressalva.status_code == 409
    assert sem_ressalva.json()["detail"] == (
        "Há 1 linha não lida nesta competência. Justifique, corrija ou feche com ressalva."
    )
    assert com_ressalva.status_code == 201
    assert com_ressalva.json()["resumo"]["linhas_nao_lidas"] == 1


def test_so_a_rodada_mais_recente_entra(conta):
    c = conta()
    banco, _ = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])  # v1 com divergência
    v2 = c.extrato("sistema", [("10.00", "a"), ("20.00", "b")])
    c.conciliar(banco, v2)

    resposta = c.fechar()

    assert resposta.status_code == 201
    resumo = resposta.json()["resumo"]
    assert resumo["pendentes"] == 0
    (par,) = resumo["pares"]
    assert par["extrato_sistema_id"] == str(v2.id) and par["rodada"] == 2


def test_competencia_pelo_periodo_inicio_do_extrato_do_banco(conta):
    c = conta()
    c.par([("10.00", "a")], [("10.00", "a")], periodo=(date(2026, 8, 25), date(2026, 9, 5)))

    assert c.fechar("2026-09").status_code == 422
    assert c.fechar("2026-08").status_code == 201

    sem_periodo = conta()
    sem_periodo.par([("10.00", "a")], [("10.00", "a")], periodo=None)
    for competencia in ("2026-08", "2026-09"):
        resposta = sem_periodo.fechar(competencia)
        assert resposta.status_code == 422
        assert resposta.json() == {"detail": "Não há conciliação nesta competência."}


@pytest.mark.parametrize("competencia", ["2026-13", "2026-9", "abc", "2026-00"])
def test_competencia_invalida_da_422_com_detail_em_texto(conta, competencia):
    c = conta()

    for resposta in (
        c.fechar(competencia),
        c.reabrir(competencia),
        c.listar(competencia=competencia),
    ):
        assert resposta.status_code == 422
        assert isinstance(resposta.json()["detail"], str)


def test_campo_extra_no_corpo_da_422(conta):
    c = conta()

    resposta = client.post(
        "/fechamentos", headers=c.headers, json={"competencia": "2026-09", "outro": 1}
    )

    assert resposta.status_code == 422


# Reabrir e histórico


def test_fechar_duas_vezes_reabrir_e_fechar_de_novo(conta):
    c = conta()
    c.par([("10.00", "a")], [("10.00", "a")])

    primeiro = c.fechar()
    repetido = c.fechar()
    reaberto = c.reabrir()
    segundo = c.fechar()

    assert primeiro.status_code == 201
    assert repetido.status_code == 409
    assert repetido.json() == {"detail": "Esta competência já está fechada."}
    assert reaberto.status_code == 200
    corpo = reaberto.json()
    assert corpo["id"] == primeiro.json()["id"]
    assert corpo["estado"] == "reaberto"
    assert corpo["reaberto_por"] == c.usuario.nome and _com_fuso(corpo["reaberto_em"])
    assert segundo.status_code == 201 and segundo.json()["id"] != primeiro.json()["id"]

    itens = c.listar().json()["itens"]
    assert [item["id"] for item in itens] == [segundo.json()["id"], primeiro.json()["id"]]
    assert [item["estado"] for item in itens] == ["fechado", "reaberto"]
    assert len(c.listar(competencia="2026-09").json()["itens"]) == 2
    assert c.listar(competencia="2026-10").json()["itens"] == []


def test_reabrir_sem_fechamento_ativo_ou_de_outra_empresa_da_404(conta):
    a, b = conta(), conta()
    a.par([("10.00", "a")], [("10.00", "a")])
    assert a.fechar().status_code == 201

    for resposta in (b.reabrir(), a.reabrir("2026-10")):
        assert resposta.status_code == 404
        assert resposta.json() == {"detail": "Não há fechamento ativo nesta competência."}


def test_sem_token_da_401():
    assert client.get("/fechamentos").status_code == 401
    assert client.post("/fechamentos", json={"competencia": "2026-09"}).status_code == 401
    assert client.delete("/fechamentos/2026-09").status_code == 401


# Bloqueio das escritas


def _execucoes(db_session, banco):
    db_session.expire_all()
    return db_session.query(ExecucaoConciliacao).filter_by(extrato_banco_id=banco.id).count()


def test_mes_fechado_bloqueia_conciliar_e_decidir_ate_reabrir(conta, db_session):
    c = conta()
    banco, sistema = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    outubro = c.extrato("banco", [("10.00", "a")], (date(2026, 10, 1), date(2026, 10, 31)))
    assert c.fechar(ressalva="pendência conhecida").status_code == 201
    execucoes_antes = _execucoes(db_session, banco)
    linhas_antes = {
        linha.id for linha in db_session.query(Conciliacao).filter_by(extrato_banco_id=banco.id)
    }

    conciliar = c.conciliar(banco, sistema, esperado=409)
    decidir = c.decidir(banco, "20.00", "conferida")

    detalhe = {"detail": "A competência 2026-09 está fechada. Reabra o fechamento para alterar."}
    assert conciliar.json() == detalhe
    assert decidir.status_code == 409 and decidir.json() == detalhe
    assert _execucoes(db_session, banco) == execucoes_antes
    assert {
        linha.id for linha in db_session.query(Conciliacao).filter_by(extrato_banco_id=banco.id)
    } == linhas_antes
    assert db_session.query(DecisaoLinha).filter_by(extrato_banco_id=banco.id).count() == 0
    c.conciliar(outubro, sistema)  # outro mês segue liberado

    assert c.reabrir().status_code == 200
    c.conciliar(banco, sistema)
    assert c.decidir(banco, "20.00", "conferida").status_code == 200


def test_isolamento_entre_empresas(conta):
    a, b = conta(), conta()
    a.par([("10.00", "a")], [("10.00", "a")])
    banco_b, sistema_b = b.par([("10.00", "a")], [("10.00", "a")])
    assert a.fechar().status_code == 201

    assert b.listar().json()["itens"] == []
    b.conciliar(banco_b, sistema_b)
    assert b.fechar().status_code == 201
    assert len(a.listar().json()["itens"]) == 1


# Travas


def _segurar(chave):
    """Conexão separada segurando a trava exclusiva até `soltar()`."""
    conexao = engine.connect()
    transacao = conexao.begin()
    conexao.execute(text("SELECT pg_advisory_xact_lock(:chave)"), {"chave": chave})

    def soltar():
        transacao.rollback()
        conexao.close()

    return soltar


def _em_segundo_plano(funcao):
    resultado = {}

    def _rodar():
        resultado["resposta"] = funcao()

    thread = threading.Thread(target=_rodar)
    thread.start()
    return thread, resultado


def _espera_ate_soltar(chave, *funcoes):
    soltar = _segurar(chave)
    try:
        execucoes = [_em_segundo_plano(funcao) for funcao in funcoes]
        time.sleep(0.5)
        assert all(thread.is_alive() for thread, _ in execucoes), "deveria estar esperando a trava"
    finally:
        soltar()
    for thread, _ in execucoes:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return [resultado["resposta"] for _, resultado in execucoes]


def test_conciliar_e_decidir_esperam_a_trava_exclusiva_do_mes(conta):
    c = conta()
    banco, sistema = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    chave = chave_do_mes(c.usuario.empresa_id, "2026-09")

    (conciliacao,) = _espera_ate_soltar(chave, lambda: c.conciliar(banco, sistema))
    (decisao,) = _espera_ate_soltar(chave, lambda: c.decidir(banco, "20.00", "conferida"))

    assert conciliacao.status_code == 201
    assert decisao.status_code == 200


def test_decisao_e_rodada_nova_esperam_a_trava_do_extrato_do_banco(conta):
    c = conta()
    banco, sistema = c.par([("10.00", "a"), ("20.00", "b")], [("10.00", "a")])
    v2 = c.extrato("sistema", [("10.00", "a")])
    chave = chave_do_extrato_banco(c.usuario.empresa_id, banco.id)

    rodada, decisao = _espera_ate_soltar(
        chave,
        lambda: c.conciliar(banco, v2),
        lambda: c.decidir(banco, "20.00", "conferida", sistema=sistema),
    )

    assert rodada.status_code == 201
    # A decisão foi gravada depois da rodada nova ou antes dela, nunca no meio:
    # se depois, registra a rodada 2; se antes, a 1.
    assert decisao.status_code == 200
    assert decisao.json()["rodada"] in (1, 2)
