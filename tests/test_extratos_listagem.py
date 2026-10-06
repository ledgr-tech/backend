"""Testes de GET /extratos (issue #70): listagem dos extratos da empresa, com
filtros, paginação e os campos derivados (linhas não lidas, período,
enviado_em e conciliado).

Precisam de Postgres real (`db_session` do conftest.py) e pulam sem ele.
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.database import engine
from app.models import LinhaInvalida
from main import app

client = TestClient(app)


def _listar(headers, **params):
    return client.get("/extratos", headers=headers, params=params)


def _ids(resposta):
    return [item["extrato_id"] for item in resposta.json()["itens"]]


def test_empresa_sem_extratos_devolve_lista_vazia(criar_empresa, auth_headers):
    resposta = _listar(auth_headers(criar_empresa().id))

    assert resposta.status_code == 200
    assert resposta.json() == {"total": 0, "limit": 20, "offset": 0, "itens": []}


def test_sem_token_da_401():
    assert client.get("/extratos").status_code == 401


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"origem": "outro"}, {"status": "lido"}],
    ids=["limit_0", "limit_101", "offset_negativo", "origem_invalida", "status_invalido"],
)
def test_parametros_invalidos_dao_422(criar_empresa, auth_headers, params):
    assert _listar(auth_headers(criar_empresa().id), **params).status_code == 422


def test_isolamento_entre_empresas(criar_empresa, criar_extrato_da_empresa, auth_headers):
    a, b = criar_empresa(), criar_empresa()
    extrato_a = criar_extrato_da_empresa(a.id, "banco")
    criar_extrato_da_empresa(b.id, "banco")
    criar_extrato_da_empresa(b.id, "sistema", status="erro")

    resposta = _listar(auth_headers(a.id))
    filtrada = _listar(auth_headers(a.id), origem="sistema", status="erro")

    assert resposta.json()["total"] == 1
    assert _ids(resposta) == [str(extrato_a.id)]
    assert filtrada.json() == {"total": 0, "limit": 20, "offset": 0, "itens": []}


def test_ordem_mais_recente_primeiro_e_paginacao(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    criados = [criar_extrato_da_empresa(empresa.id, "banco") for _ in range(5)]

    tudo = _listar(headers, limit=100)
    paginas = [_listar(headers, limit=2, offset=offset) for offset in (0, 2, 4)]

    assert _ids(tudo) == [str(extrato.id) for extrato in reversed(criados)]
    assert [len(pagina.json()["itens"]) for pagina in paginas] == [2, 2, 1]
    assert [i for pagina in paginas for i in _ids(pagina)] == _ids(tudo)
    assert {pagina.json()["total"] for pagina in paginas} == {5}
    assert tudo.json()["total"] == 5


def test_filtros_por_origem_e_status(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    banco_ok = criar_extrato_da_empresa(empresa.id, "banco", status="concluido")
    banco_erro = criar_extrato_da_empresa(empresa.id, "banco", status="erro")
    sistema_parcial = criar_extrato_da_empresa(empresa.id, "sistema", status="concluido_com_erros")
    sistema_pendente = criar_extrato_da_empresa(empresa.id, "sistema", status="pendente")

    def ids(**params):
        resposta = _listar(headers, limit=100, **params)
        assert resposta.json()["total"] == len(resposta.json()["itens"])
        return set(_ids(resposta))

    assert ids(origem="banco") == {str(banco_ok.id), str(banco_erro.id)}
    assert ids(origem="sistema") == {str(sistema_parcial.id), str(sistema_pendente.id)}
    assert ids(status="erro") == {str(banco_erro.id)}
    assert ids(status=["erro", "concluido_com_erros"]) == {
        str(banco_erro.id),
        str(sistema_parcial.id),
    }
    assert ids(origem="sistema", status=["erro", "concluido_com_erros"]) == {
        str(sistema_parcial.id)
    }
    paginada = _listar(headers, status=["erro", "concluido_com_erros"], limit=1)
    assert paginada.json()["total"] == 2 and len(paginada.json()["itens"]) == 1


def test_campos_do_item(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    processado = criar_extrato_da_empresa(empresa.id, "banco", status="concluido_com_erros")
    processado.quantidade_lancamentos = 42
    processado.periodo_inicio, processado.periodo_fim = date(2026, 9, 1), date(2026, 9, 30)
    for identificador in ("3", "7"):
        db_session.add(
            LinhaInvalida(extrato_id=processado.id, identificador=identificador, motivo="x")
        )
    pendente = criar_extrato_da_empresa(empresa.id, "sistema", status="pendente")
    pendente.quantidade_lancamentos = None
    db_session.commit()

    itens = {item["extrato_id"]: item for item in _listar(auth_headers(empresa.id)).json()["itens"]}

    item = itens[str(processado.id)]
    assert item["nome_arquivo"] == processado.nome_arquivo
    assert item["origem"] == "banco" and item["formato"] == processado.formato
    assert item["status"] == "concluido_com_erros"
    assert item["quantidade_lancamentos"] == 42
    assert item["linhas_nao_lidas"] == 2
    assert item["periodo_inicio"] == "2026-09-01" and item["periodo_fim"] == "2026-09-30"
    assert datetime.fromisoformat(item["enviado_em"]).tzinfo is not None
    assert item["conciliado"] is False

    vazio = itens[str(pendente.id)]
    assert vazio["quantidade_lancamentos"] is None
    assert vazio["linhas_nao_lidas"] == 0
    assert vazio["periodo_inicio"] is None and vazio["periodo_fim"] is None


def test_conciliado_vale_para_o_extrato_do_banco_e_o_do_sistema(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    avulso = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a")])
    inserir_lancamentos(sistema, [("10.00", "a")])

    def conciliados():
        return {item["extrato_id"]: item["conciliado"] for item in _listar(headers).json()["itens"]}

    assert set(conciliados().values()) == {False}
    resposta = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert resposta.status_code == 201

    depois = conciliados()
    assert depois[str(banco.id)] is True
    assert depois[str(sistema.id)] is True
    assert depois[str(avulso.id)] is False


def test_numero_fixo_de_consultas(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    for indice in range(50):
        extrato = criar_extrato_da_empresa(empresa.id, "banco" if indice % 2 else "sistema")
        db_session.add(LinhaInvalida(extrato_id=extrato.id, identificador="1", motivo="x"))
    db_session.commit()

    def consultas(limit):
        contadas = []

        def _contar(conn, cursor, statement, parameters, context, executemany):
            contadas.append(statement)

        event.listen(engine, "before_cursor_execute", _contar)
        try:
            resposta = _listar(headers, limit=limit)
        finally:
            event.remove(engine, "before_cursor_execute", _contar)
        assert len(resposta.json()["itens"]) == limit
        return len(contadas)

    assert consultas(5) == consultas(50)
