"""Testes das decisões por linha (issue #83): regras de app/services/decisoes.py,
os campos `chave`/`decisao`/`eventos` de `GET /conciliacoes/{extrato_id}` e
`POST /conciliacoes/{extrato_id}/decisoes`.

Os testes de função pura rodam sem banco; os demais usam Postgres real
(`db_session` do conftest.py) e pulam sem ele. `justificadas` em
`GET /execucoes` tem testes em tests/test_execucoes.py.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.database import engine
from app.models import DecisaoLinha
from app.services.decisoes import chave_da_linha, decisao_vigente
from main import app

client = TestClient(app)


# chave_da_linha e decisao_vigente (sem banco)


def test_chave_de_linha_com_lancamento_do_banco_e_o_id_dele():
    lancamento_id = uuid.uuid4()

    assert chave_da_linha(lancamento_id, Decimal("10.00"), date(2026, 9, 5), "x", 1) == str(
        lancamento_id
    )


def test_chave_so_do_sistema_ignora_diferencas_de_caixa_e_espaco():
    a = chave_da_linha(None, Decimal("77.00"), date(2026, 9, 5), "Pagamento  Fornecedor", 1)
    b = chave_da_linha(None, Decimal("77.00"), date(2026, 9, 5), " pagamento fornecedor ", 1)

    assert a == b
    assert len(a) == 64


def test_chave_so_do_sistema_muda_com_a_ocorrencia():
    primeira = chave_da_linha(None, Decimal("77.00"), date(2026, 9, 5), "x", 1)
    segunda = chave_da_linha(None, Decimal("77.00"), date(2026, 9, 5), "x", 2)

    assert primeira != segunda


def _evento(tipo):
    return SimpleNamespace(tipo=tipo)


@pytest.mark.parametrize(
    ("tipos", "esperado"),
    [
        ([], None),
        (["conferida"], "conferida"),
        (["conferida", "conferencia_desfeita"], None),
        (["conferida", "justificada"], "justificada"),
        (["justificada", "justificativa_desfeita"], None),
        (["justificada", "justificativa_desfeita", "conferida"], "conferida"),
    ],
)
def test_decisao_vigente(tipos, esperado):
    vigente = decisao_vigente([_evento(tipo) for tipo in tipos])

    assert (vigente.tipo if vigente else None) == esperado


# Com banco


@pytest.fixture
def cenario(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """Usuário, extrato do banco e uma fábrica de versões do extrato do
    sistema. O par padrão tem um match exato (10.00 a), uma linha só do banco
    (20.00 b) e uma só do sistema (99.00 so sistema), as duas divergentes."""
    usuario = criar_usuario()
    empresa_id = usuario.empresa_id
    banco = criar_extrato_da_empresa(empresa_id, "banco")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b")])

    def novo_sistema(itens=(("10.00", "a"), ("99.00", "so sistema"))):
        sistema = criar_extrato_da_empresa(empresa_id, "sistema")
        inserir_lancamentos(sistema, list(itens))
        return sistema

    token = gerar_token(empresa_id, usuario_id=usuario.id)
    return SimpleNamespace(
        usuario=usuario,
        empresa_id=empresa_id,
        banco=banco,
        novo_sistema=novo_sistema,
        headers={"Authorization": f"Bearer {token}"},
    )


def _conciliar(c, sistema):
    resposta = client.post(
        "/conciliacoes",
        headers=c.headers,
        json={"extrato_banco_id": str(c.banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert resposta.status_code == 201


def _itens(c, sistema=None, **params):
    if sistema is not None:
        params["extrato_sistema_id"] = str(sistema.id)
    resposta = client.get(f"/conciliacoes/{c.banco.id}", headers=c.headers, params=params)
    assert resposta.status_code == 200
    return resposta.json()["itens"]


def _linha(itens, valor_banco=None, valor_sistema=None):
    for item in itens:
        if (
            valor_banco
            and item["lancamento_banco"]
            and item["lancamento_banco"]["valor"] == (valor_banco)
        ):
            return item
        if (
            valor_sistema
            and item["lancamento_banco"] is None
            and item["lancamento_sistema"]["valor"] == valor_sistema
        ):
            return item
    raise AssertionError("linha não encontrada")


def _decidir(c, chave, tipo, texto=None, extrato_id=None, headers=None):
    return client.post(
        f"/conciliacoes/{extrato_id or c.banco.id}/decisoes",
        headers=c.headers if headers is None else headers,
        json={"chave": chave, "tipo": tipo, "texto": texto},
    )


def test_get_sem_decisao_traz_chave_decisao_null_e_eventos_vazios(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)

    itens = _itens(cenario, sistema)

    assert len(itens) == 3
    for item in itens:
        assert "decisao" in item and item["decisao"] is None
        assert item["eventos"] == []
        assert item["chave"]
    assert len({item["chave"] for item in itens}) == 3
    linha_banco = _linha(itens, valor_banco="20.00")
    assert linha_banco["chave"] == linha_banco["lancamento_banco"]["id"]


def test_chave_unica_com_duplicado_tarifa_e_linhas_identicas_do_sistema(
    db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token
):
    usuario = criar_usuario()
    banco = criar_extrato_da_empresa(usuario.empresa_id, "banco")
    sistema = criar_extrato_da_empresa(usuario.empresa_id, "sistema")
    inserir_lancamentos(
        banco, [("150.00", "c"), ("150.00", "c"), ("150.00", "c"), ("-5.00", "Tarifa pacote")]
    )
    inserir_lancamentos(sistema, [("150.00", "c"), ("150.00", "c"), ("77.00", "x"), ("77.00", "x")])
    token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
    c = SimpleNamespace(usuario=usuario, banco=banco, headers={"Authorization": f"Bearer {token}"})
    _conciliar(c, sistema)

    itens = _itens(c, sistema)

    assert {"duplicado", "tarifa_bancaria"} <= {item["status"] for item in itens}
    assert len({item["chave"] for item in itens}) == len(itens) == 6
    so_sistema = [item["chave"] for item in itens if item["lancamento_banco"] is None]
    assert len(so_sistema) == 2 and so_sistema[0] != so_sistema[1]


def test_fluxo_conferir_justificar_e_desfazer(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)
    chave = _linha(_itens(cenario, sistema), valor_banco="20.00")["chave"]

    conferida = _decidir(cenario, chave, "conferida", texto="ignorado")
    assert conferida.status_code == 200
    corpo = conferida.json()
    assert corpo["tipo"] == "conferida"
    assert corpo["texto"] is None
    assert corpo["autor"] == cenario.usuario.nome
    assert corpo["rodada"] == 1
    assert datetime.fromisoformat(corpo["em"]).tzinfo is not None

    desfeita = _decidir(cenario, chave, "conferencia_desfeita")
    assert desfeita.status_code == 200 and desfeita.json() is None

    justificada = _decidir(cenario, chave, "justificada", texto="  Juros de atraso.  ")
    assert justificada.status_code == 200
    assert justificada.json()["tipo"] == "justificada"
    assert justificada.json()["texto"] == "Juros de atraso."

    item = _linha(_itens(cenario, sistema), valor_banco="20.00")
    assert item["decisao"]["tipo"] == "justificada"
    assert [e["tipo"] for e in item["eventos"]] == [
        "conferida",
        "conferencia_desfeita",
        "justificada",
    ]
    momentos = [datetime.fromisoformat(e["em"]) for e in item["eventos"]]
    assert momentos == sorted(momentos)
    assert all(m.tzinfo is not None for m in momentos)

    assert _decidir(cenario, chave, "justificativa_desfeita").json() is None
    item = _linha(_itens(cenario, sistema), valor_banco="20.00")
    assert item["decisao"] is None
    assert len(item["eventos"]) == 4


@pytest.mark.parametrize("texto", [None, "", "   "])
def test_justificada_sem_texto_da_422_com_detail_em_texto(cenario, texto):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)
    chave = _linha(_itens(cenario, sistema), valor_banco="20.00")["chave"]

    resposta = _decidir(cenario, chave, "justificada", texto=texto)

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": "A justificativa precisa de um texto."}


def test_justificada_acima_de_1000_caracteres_da_422(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)
    chave = _linha(_itens(cenario, sistema), valor_banco="20.00")["chave"]

    resposta = _decidir(cenario, chave, "justificada", texto="x" * 1001)

    assert resposta.status_code == 422
    assert isinstance(resposta.json()["detail"], str)
    assert _decidir(cenario, chave, "justificada", texto="x" * 1000).status_code == 200


def test_desfazer_sem_a_decisao_do_tipo_da_422(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)
    chave = _linha(_itens(cenario, sistema), valor_banco="20.00")["chave"]

    sem_conferencia = _decidir(cenario, chave, "conferencia_desfeita")
    sem_justificativa = _decidir(cenario, chave, "justificativa_desfeita")
    _decidir(cenario, chave, "justificada", texto="motivo")
    conferencia_com_justificativa = _decidir(cenario, chave, "conferencia_desfeita")

    assert sem_conferencia.status_code == 422
    assert sem_conferencia.json() == {"detail": "Não há conferência para desfazer nesta linha."}
    assert sem_justificativa.status_code == 422
    assert sem_justificativa.json() == {"detail": "Não há justificativa para desfazer nesta linha."}
    assert conferencia_com_justificativa.status_code == 422


def test_linha_que_nao_diverge_da_422(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)
    chave = _linha(_itens(cenario, sistema), valor_banco="10.00")["chave"]

    resposta = _decidir(cenario, chave, "conferida")

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": "Só é possível decidir sobre uma linha que diverge."}


@pytest.mark.parametrize("chave", [str(uuid.uuid4()), "0" * 64, "nao-existe"])
def test_chave_inexistente_da_404(cenario, chave):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)

    resposta = _decidir(cenario, chave, "conferida")

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Esta linha não está mais nesta conciliação."}


def test_extrato_do_banco_sem_execucao_da_404(cenario):
    resposta = _decidir(cenario, str(uuid.uuid4()), "conferida")

    assert resposta.status_code == 404


def test_extrato_do_sistema_na_url_da_422(cenario):
    sistema = cenario.novo_sistema()
    _conciliar(cenario, sistema)

    resposta = _decidir(cenario, str(uuid.uuid4()), "conferida", extrato_id=sistema.id)

    assert resposta.status_code == 422


def test_extrato_de_outra_empresa_da_404(cenario, criar_empresa, criar_extrato_da_empresa):
    outro = criar_extrato_da_empresa(criar_empresa().id, "banco")

    resposta = _decidir(cenario, str(uuid.uuid4()), "conferida", extrato_id=outro.id)

    assert resposta.status_code == 404


def test_sem_token_da_401(cenario):
    assert _decidir(cenario, "x", "conferida", headers={}).status_code == 401


def test_decisao_sobrevive_a_rodadas_e_registra_a_rodada_certa(cenario, db_session):
    v1 = cenario.novo_sistema()
    _conciliar(cenario, v1)
    itens_v1 = _itens(cenario, v1)
    chave_banco = _linha(itens_v1, valor_banco="20.00")["chave"]
    chave_sistema_v1 = _linha(itens_v1, valor_sistema="99.00")["chave"]
    assert _decidir(cenario, chave_banco, "justificada", texto="motivo").json()["rodada"] == 1

    _conciliar(cenario, v1)  # mesmo par de novo: não cria rodada
    assert _linha(_itens(cenario, v1), valor_banco="20.00")["decisao"]["tipo"] == "justificada"
    assert _decidir(cenario, chave_banco, "conferida").json()["rodada"] == 1

    v2 = cenario.novo_sistema()  # nova versão do extrato do sistema, mesmo conteúdo
    _conciliar(cenario, v2)
    itens_v2 = _itens(cenario, v2)
    assert _linha(itens_v2, valor_banco="20.00")["decisao"]["tipo"] == "conferida"
    assert _linha(itens_v2, valor_sistema="99.00")["chave"] == chave_sistema_v1
    assert _decidir(cenario, chave_banco, "conferida").json()["rodada"] == 2

    _conciliar(cenario, v1)  # reconciliar a v1 depois da v2 não volta pra rodada 1
    assert _decidir(cenario, chave_sistema_v1, "conferida").json()["rodada"] == 2

    db_session.expire_all()
    ultimo = (
        db_session.query(DecisaoLinha)
        .filter_by(empresa_id=cenario.empresa_id)
        .order_by(DecisaoLinha.em.desc())
        .first()
    )
    assert ultimo.extrato_sistema_id == v2.id
    assert ultimo.usuario_id == cenario.usuario.id


def test_isolamento_entre_empresas_com_chaves_iguais(
    db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token
):
    cenarios = []
    for _ in range(2):
        usuario = criar_usuario()
        banco = criar_extrato_da_empresa(usuario.empresa_id, "banco")
        sistema = criar_extrato_da_empresa(usuario.empresa_id, "sistema")
        inserir_lancamentos(banco, [("10.00", "a")])
        inserir_lancamentos(sistema, [("10.00", "a"), ("99.00", "so sistema")])
        token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
        c = SimpleNamespace(
            usuario=usuario, banco=banco, headers={"Authorization": f"Bearer {token}"}
        )
        _conciliar(c, sistema)
        cenarios.append((c, sistema))
    (a, sistema_a), (b, sistema_b) = cenarios
    chave_a = _linha(_itens(a, sistema_a), valor_sistema="99.00")["chave"]
    chave_b = _linha(_itens(b, sistema_b), valor_sistema="99.00")["chave"]
    assert chave_a == chave_b

    assert _decidir(a, chave_a, "justificada", texto="motivo").status_code == 200

    linha_b = _linha(_itens(b, sistema_b), valor_sistema="99.00")
    assert linha_b["decisao"] is None and linha_b["eventos"] == []
    assert _decidir(b, chave_b, "justificativa_desfeita").status_code == 422


def test_eventos_da_pagina_saem_com_numero_fixo_de_consultas(
    db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token
):
    usuario = criar_usuario()
    banco = criar_extrato_da_empresa(usuario.empresa_id, "banco")
    sistema = criar_extrato_da_empresa(usuario.empresa_id, "sistema")
    inserir_lancamentos(banco, [(f"{indice + 1}.00", f"linha {indice}") for indice in range(60)])
    inserir_lancamentos(sistema, [("0.50", "so sistema")])
    token = gerar_token(usuario.empresa_id, usuario_id=usuario.id)
    c = SimpleNamespace(usuario=usuario, banco=banco, headers={"Authorization": f"Bearer {token}"})
    _conciliar(c, sistema)
    for item in _itens(c, sistema, limit=5):
        assert _decidir(c, item["chave"], "conferida").status_code == 200

    def _consultas(limit):
        contadas = []

        def _contar(conn, cursor, statement, parameters, context, executemany):
            contadas.append(statement)

        event.listen(engine, "before_cursor_execute", _contar)
        try:
            itens = _itens(c, sistema, limit=limit)
        finally:
            event.remove(engine, "before_cursor_execute", _contar)
        return len(contadas), itens

    consultas_5, itens_5 = _consultas(5)
    consultas_50, itens_50 = _consultas(50)

    assert len(itens_5) == 5 and len(itens_50) == 50
    assert all(item["decisao"]["tipo"] == "conferida" for item in itens_5)
    assert consultas_5 == consultas_50
