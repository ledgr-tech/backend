"""Revisão da importação via IA (issue #159, ADR-013): listar, corrigir,
remover, adicionar e confirmar o rascunho."""

import json
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app.core.auth import CABECALHO_EMPRESA
from app.core.database import engine
from app.models import Extrato, Lancamento
from app.services.normalizacao import _calcular_hash_dedup
from main import app

client = TestClient(app)

OFX = (Path(__file__).parent / "fixtures" / "extrato_valido.ofx").read_bytes()
LANCAMENTOS = [
    {"data": "2026-09-05", "valor": "-100.00", "descricao": "Aluguel"},
    {"data": "2026-09-02", "valor": "500.00", "descricao": "Recebimento"},
    {"data": "2026-09-10", "valor": "-30.00", "descricao": "Tarifa"},
]
NAO_ENCONTRADA = {"detail": "Importação não encontrada."}
CONFIRMADA = {"detail": "Esta importação já foi confirmada."}


@pytest.fixture
def conta(db_session, criar_usuario, gerar_token, criar_empresa):
    def _nova(outra: bool = False, papel: str | None = None):
        admin = criar_usuario(nome="Ana Admin")
        usuario = admin
        if papel is not None:
            usuario = criar_usuario(conta_id=admin.conta_id, nome="Alan Analista", papel=papel)
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        empresa_id = empresa_unica(db_session, admin.conta_id)
        contexto = SimpleNamespace(
            usuario=usuario, empresa_id=empresa_id, headers=cabecalhos(token, empresa_id)
        )
        if outra:
            segunda = criar_empresa("Cliente 2", conta_id=admin.conta_id)
            contexto.headers_outra = cabecalhos(token, segunda.id)
        return contexto

    return _nova


def _importar(headers, lancamentos=LANCAMENTOS) -> str:
    conteudo = json.dumps({"versao": "1", "lancamentos": lancamentos}).encode()
    resposta = client.post(
        "/importacoes-ia",
        headers=headers,
        files={"arquivo": ("relatorio.json", conteudo, "application/json")},
    )
    assert resposta.status_code == 201, resposta.text
    return resposta.json()["importacao_id"]


def _revisar(headers, importacao_id, **params):
    return client.get(f"/importacoes-ia/{importacao_id}", headers=headers, params=params)


def _ids(headers, importacao_id) -> list[str]:
    return [item["id"] for item in _revisar(headers, importacao_id).json()["itens"]]


def _corrigir(headers, importacao_id, lancamento_id, **campos):
    return client.patch(
        f"/importacoes-ia/{importacao_id}/lancamentos/{lancamento_id}",
        headers=headers,
        json=campos,
    )


def _remover(headers, importacao_id, lancamento_id):
    return client.delete(
        f"/importacoes-ia/{importacao_id}/lancamentos/{lancamento_id}", headers=headers
    )


def _adicionar(headers, importacao_id, **campos):
    return client.post(f"/importacoes-ia/{importacao_id}/lancamentos", headers=headers, json=campos)


def _confirmar(headers, importacao_id):
    return client.post(f"/importacoes-ia/{importacao_id}/confirmar", headers=headers)


def _linhas(db_session, importacao_id) -> list[Lancamento]:
    db_session.expire_all()
    return db_session.scalars(
        select(Lancamento)
        .where(Lancamento.extrato_id == uuid.UUID(importacao_id))
        .order_by(Lancamento.posicao)
    ).all()


def _envelhecer(db_session, importacao_id, dias):
    db_session.execute(
        update(Extrato)
        .where(Extrato.id == uuid.UUID(importacao_id))
        .values(criado_em=func.now() - timedelta(days=dias))
    )
    db_session.commit()


# --- listar ---------------------------------------------------------------------------


def test_lista_o_resumo_e_as_linhas_na_ordem_do_arquivo(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    resposta = _revisar(c.headers, importacao_id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert (corpo["importacao_id"], corpo["status"]) == (importacao_id, "rascunho")
    assert (corpo["total"], corpo["limit"], corpo["offset"]) == (3, 100, 0)
    resumo = corpo["resumo"]
    assert (resumo["quantidade"], resumo["total_entradas"], resumo["total_saidas"]) == (
        3,
        "500.00",
        "-130.00",
    )
    assert (resumo["periodo_inicio"], resumo["periodo_fim"]) == ("2026-09-02", "2026-09-10")
    assert "expira_em" in resumo
    assert [
        (item["numero"], item["data"], item["valor"], item["descricao"]) for item in corpo["itens"]
    ] == [
        (1, "2026-09-05", "-100.00", "Aluguel"),
        (2, "2026-09-02", "500.00", "Recebimento"),
        (3, "2026-09-10", "-30.00", "Tarifa"),
    ]
    assert set(corpo["itens"][0]) == {"id", "numero", "data", "valor", "descricao"}


def test_pagina_com_numero_continuo(db_session, conta):
    c = conta()
    linhas = [
        {"data": "2026-09-01", "valor": f"-{i + 1}.00", "descricao": f"Linha {i}"}
        for i in range(25)
    ]
    importacao_id = _importar(c.headers, linhas)

    primeira = _revisar(c.headers, importacao_id, limit=10).json()
    terceira = _revisar(c.headers, importacao_id, limit=10, offset=20).json()

    assert [item["numero"] for item in primeira["itens"]] == list(range(1, 11))
    assert [item["numero"] for item in terceira["itens"]] == list(range(21, 26))
    assert [item["descricao"] for item in terceira["itens"]] == [
        f"Linha {i}" for i in range(20, 25)
    ]
    assert primeira["total"] == terceira["total"] == 25
    assert _revisar(c.headers, importacao_id, limit=0).status_code == 422
    assert _revisar(c.headers, importacao_id, limit=1001).status_code == 422


# --- corrigir -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({"data": "2026-09-06"}, ("2026-09-06", "-100.00", "Aluguel")),
        ({"valor": "100.00"}, ("2026-09-05", "100.00", "Aluguel")),
        ({"descricao": "  Aluguel de setembro "}, ("2026-09-05", "-100.00", "Aluguel de setembro")),
        (
            {"data": "2026-09-30", "valor": "-99.99", "descricao": "Outro"},
            ("2026-09-30", "-99.99", "Outro"),
        ),
    ],
    ids=["data", "valor", "descricao", "os_tres"],
)
def test_corrige_cada_campo_e_devolve_o_resumo(db_session, conta, campos, esperado):
    c = conta()
    importacao_id = _importar(c.headers)
    primeira = _ids(c.headers, importacao_id)[0]

    resposta = _corrigir(c.headers, importacao_id, primeira, **campos)

    assert resposta.status_code == 200, resposta.text
    linha = resposta.json()["lancamento"]
    assert (linha["id"], linha["numero"]) == (primeira, 1)
    assert (linha["data"], linha["valor"], linha["descricao"]) == esperado
    assert resposta.json()["resumo"] == _revisar(c.headers, importacao_id).json()["resumo"]
    gravada, *_ = _linhas(db_session, importacao_id)
    assert gravada.tipo == ("credito" if not esperado[1].startswith("-") else "debito")
    assert gravada.hash_dedup == _calcular_hash_dedup(
        gravada.valor, gravada.data, gravada.descricao, gravada.extrato_id, gravada.ocorrencia
    )


def test_resumo_acompanha_a_correcao(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    tarifa = _ids(c.headers, importacao_id)[2]

    resumo = _corrigir(c.headers, importacao_id, tarifa, valor="30.00", data="2026-10-01").json()[
        "resumo"
    ]

    assert (resumo["total_entradas"], resumo["total_saidas"]) == ("530.00", "-100.00")
    assert resumo["periodo_fim"] == "2026-10-01"


@pytest.mark.parametrize(
    ("campos", "erros"),
    [
        (
            {"valor": "1.234,56"},
            [("valor", 2, "use número com ponto decimal e no máximo duas casas")],
        ),
        ({"valor": -12.5}, [("valor", 2, "(veio -12.5, sem aspas)")]),
        ({"data": "05/09/2026"}, [("data", 2, "use o formato AAAA-MM-DD")]),
        ({"descricao": " "}, [("descricao", 2, "a descrição está vazia")]),
        ({"valor": "0"}, [("valor", 2, "valor zero não é lançamento")]),
        (
            {"saldo": "1"},
            [("saldo", 2, "campo não aceito"), ("(lançamento)", 2, "informe data, valor")],
        ),
        ({}, [("(lançamento)", 2, "informe data, valor ou descricao")]),
        (
            {"data": "ontem", "valor": "dez"},
            [("data", 2, "AAAA-MM-DD"), ("valor", 2, "ponto decimal")],
        ),
    ],
    ids=["virgula", "numero", "data", "vazia", "zero", "campo_novo", "nada", "dois"],
)
def test_correcao_invalida_e_422_e_nao_muda_nada(db_session, conta, campos, erros):
    c = conta()
    importacao_id = _importar(c.headers)
    segunda = _ids(c.headers, importacao_id)[1]
    antes = _revisar(c.headers, importacao_id).json()

    resposta = _corrigir(c.headers, importacao_id, segunda, **campos)

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["detail"] == (
        "O lançamento não está no formato do Ledgr. Corrija os pontos abaixo."
    )
    assert corpo["total_erros"] == len(erros)
    for erro, (caminho, numero, trecho) in zip(corpo["erros"], erros, strict=True):
        assert (erro["caminho"], erro["lancamento"]) == (caminho, numero)
        assert trecho in erro["mensagem"]
    assert _revisar(c.headers, importacao_id).json() == antes


def test_correcao_que_iguala_duas_linhas_usa_ocorrencia_livre(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    aluguel, recebimento, _ = _ids(c.headers, importacao_id)

    resposta = _corrigir(
        c.headers,
        importacao_id,
        recebimento,
        data="2026-09-05",
        valor="-100.00",
        descricao="ALUGUEL",
    )

    assert resposta.status_code == 200
    linhas = {str(linha.id): linha for linha in _linhas(db_session, importacao_id)}
    assert (linhas[aluguel].ocorrencia, linhas[recebimento].ocorrencia) == (1, 2)
    assert linhas[aluguel].hash_dedup != linhas[recebimento].hash_dedup


# --- remover e adicionar --------------------------------------------------------------


def test_remove_e_renumera_na_listagem(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    aluguel, recebimento, tarifa = _ids(c.headers, importacao_id)

    resposta = _remover(c.headers, importacao_id, recebimento)

    assert resposta.status_code == 204
    corpo = _revisar(c.headers, importacao_id).json()
    assert [(item["id"], item["numero"]) for item in corpo["itens"]] == [
        (aluguel, 1),
        (tarifa, 2),
    ]
    assert (corpo["resumo"]["quantidade"], corpo["resumo"]["total_entradas"]) == (2, "0.00")
    assert _remover(c.headers, importacao_id, recebimento).json() == {
        "detail": "Lançamento não encontrado."
    }


def test_remover_todas_deixa_o_rascunho_vazio(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    for lancamento_id in _ids(c.headers, importacao_id):
        assert _remover(c.headers, importacao_id, lancamento_id).status_code == 204

    corpo = _revisar(c.headers, importacao_id).json()
    assert corpo["itens"] == []
    assert corpo["resumo"] == {
        **corpo["resumo"],
        "quantidade": 0,
        "total_entradas": "0.00",
        "total_saidas": "0.00",
        "periodo_inicio": None,
        "periodo_fim": None,
    }
    confirmar = _confirmar(c.headers, importacao_id)
    assert confirmar.status_code == 422
    assert confirmar.json() == {
        "detail": "A importação não tem nenhum lançamento. Adicione um lançamento ou descarte a "
        "importação."
    }


def test_adiciona_no_fim_da_ordem(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    _remover(c.headers, importacao_id, _ids(c.headers, importacao_id)[2])

    resposta = _adicionar(
        c.headers, importacao_id, data="2026-08-31", valor="-7.50", descricao="IOF"
    )

    assert resposta.status_code == 201, resposta.text
    linha = resposta.json()["lancamento"]
    assert (linha["numero"], linha["data"], linha["valor"]) == (3, "2026-08-31", "-7.50")
    assert resposta.json()["resumo"]["periodo_inicio"] == "2026-08-31"
    assert [item["descricao"] for item in _revisar(c.headers, importacao_id).json()["itens"]] == [
        "Aluguel",
        "Recebimento",
        "IOF",
    ]
    # No fim da ordem: a maior posição que sobrou (1) mais um.
    nova = _linhas(db_session, importacao_id)[-1]
    assert (nova.posicao, nova.tipo, nova.ocorrencia) == (2, "debito", 1)


def test_adicionar_exige_os_tres_campos(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    resposta = _adicionar(c.headers, importacao_id, valor="10.00")

    assert resposta.status_code == 422
    assert [(e["caminho"], e["mensagem"]) for e in resposta.json()["erros"]] == [
        ("data", "campo obrigatório"),
        ("descricao", "campo obrigatório"),
    ]
    assert resposta.json()["erros"][0]["lancamento"] is None
    assert _revisar(c.headers, importacao_id).json()["total"] == 3


def test_adicionar_linha_igual_a_outra_usa_ocorrencia_livre(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    resposta = _adicionar(c.headers, importacao_id, **LANCAMENTOS[0])

    assert resposta.status_code == 201
    linhas = _linhas(db_session, importacao_id)
    assert [linha.ocorrencia for linha in linhas] == [1, 1, 1, 2]


def test_adicionar_num_rascunho_vazio(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    for lancamento_id in _ids(c.headers, importacao_id):
        _remover(c.headers, importacao_id, lancamento_id)

    resposta = _adicionar(c.headers, importacao_id, **LANCAMENTOS[1])

    assert resposta.json()["lancamento"]["numero"] == 1
    assert _linhas(db_session, importacao_id)[0].posicao == 0


# --- confirmar ------------------------------------------------------------------------


def test_confirmar_vira_extrato_e_grava_quem_confirmou(db_session, conta):
    c = conta(papel="analista")
    importacao_id = _importar(c.headers)
    _remover(c.headers, importacao_id, _ids(c.headers, importacao_id)[1])

    resposta = _confirmar(c.headers, importacao_id)

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo == client.get(f"/extratos/{importacao_id}", headers=c.headers).json()
    assert (corpo["status"], corpo["origem"], corpo["quantidade_lancamentos"]) == (
        "concluido",
        "sistema",
        2,
    )
    assert (corpo["periodo_inicio"], corpo["periodo_fim"]) == ("2026-09-05", "2026-09-10")
    db_session.expire_all()
    extrato = db_session.get(Extrato, uuid.UUID(importacao_id))
    assert (extrato.confirmado_por_id, extrato.confirmado_por_nome) == (
        c.usuario.id,
        "Alan Analista",
    )
    agora = db_session.scalar(select(func.now()))
    assert abs(agora - extrato.confirmado_em) < timedelta(minutes=1)
    lista = client.get("/extratos", headers=c.headers).json()
    assert [item["extrato_id"] for item in lista["itens"]] == [importacao_id]
    assert lista["itens"][0]["formato"] == "json"


def test_confirmar_renumera_de_forma_deterministica(db_session, conta):
    """Duas linhas que ficam iguais por correção, com uma linha nova igual a
    elas no meio da ordem: a renumeração segue `posicao`, e o hash é o que a
    normalização de um arquivo nessa ordem daria."""
    c = conta()
    linhas = [
        {"data": "2026-09-05", "valor": "-10.00", "descricao": "Pix"},
        {"data": "2026-09-06", "valor": "-20.00", "descricao": "Boleto"},
        {"data": "2026-09-07", "valor": "-30.00", "descricao": "TED"},
    ]
    importacao_id = _importar(c.headers, linhas)
    pix, boleto, ted = _ids(c.headers, importacao_id)
    # A 3ª fica igual à 1ª (ocorrência livre 2), e depois a 1ª deixa de ser e
    # volta: na edição ela ganha a ocorrência 3, fora de ordem.
    _corrigir(c.headers, importacao_id, ted, data="2026-09-05", valor="-10.00", descricao="pix")
    _corrigir(c.headers, importacao_id, pix, descricao="Pix errado")
    _corrigir(c.headers, importacao_id, pix, descricao="PIX")
    _remover(c.headers, importacao_id, boleto)
    _adicionar(c.headers, importacao_id, data="2026-09-05", valor="-10.00", descricao="Pix")
    antes = {str(linha.id): linha.ocorrencia for linha in _linhas(db_session, importacao_id)}
    assert (antes[pix], antes[ted]) == (3, 2)

    assert _confirmar(c.headers, importacao_id).status_code == 200

    depois = _linhas(db_session, importacao_id)
    assert [(str(linha.id), linha.ocorrencia) for linha in depois][:2] == [(pix, 1), (ted, 2)]
    assert [linha.ocorrencia for linha in depois] == [1, 2, 3]
    for linha in depois:
        assert linha.hash_dedup == _calcular_hash_dedup(
            linha.valor, linha.data, linha.descricao, linha.extrato_id, linha.ocorrencia
        )
    assert len({linha.hash_dedup for linha in depois}) == 3


def test_extrato_confirmado_concilia_com_o_do_banco(db_session, conta):
    c = conta()
    banco = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "banco"},
        files={"arquivo": ("banco.ofx", OFX, "application/x-ofx")},
    ).json()["extrato_id"]
    importacao_id = _importar(c.headers)
    antes = client.post(
        "/conciliacoes",
        headers=c.headers,
        json={"extrato_banco_id": banco, "extrato_sistema_id": importacao_id},
    )
    assert antes.status_code == 404

    _confirmar(c.headers, importacao_id)
    depois = client.post(
        "/conciliacoes",
        headers=c.headers,
        json={"extrato_banco_id": banco, "extrato_sistema_id": importacao_id},
    )

    assert depois.status_code == 201, depois.text
    assert depois.json()["extrato_sistema_id"] == importacao_id
    (execucao,) = client.get("/execucoes", headers=c.headers).json()["itens"]
    assert execucao["extrato_sistema_id"] == importacao_id


def test_segunda_confirmacao_e_409_e_as_outras_rotas_tambem(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    primeira_linha = _ids(c.headers, importacao_id)[0]
    assert _confirmar(c.headers, importacao_id).status_code == 200

    respostas = [
        _confirmar(c.headers, importacao_id),
        _revisar(c.headers, importacao_id),
        _corrigir(c.headers, importacao_id, primeira_linha, valor="1.00"),
        _remover(c.headers, importacao_id, primeira_linha),
        _adicionar(c.headers, importacao_id, **LANCAMENTOS[0]),
    ]

    assert [(r.status_code, r.json()) for r in respostas] == [(409, CONFIRMADA)] * 5
    # O descarte da #158 continua 404, e o extrato fica.
    descarte = client.delete(f"/importacoes-ia/{importacao_id}", headers=c.headers)
    assert (descarte.status_code, descarte.json()) == (404, NAO_ENCONTRADA)
    assert client.get(f"/extratos/{importacao_id}", headers=c.headers).status_code == 200


def test_extrato_comum_nao_e_importacao(db_session, conta):
    c = conta()
    extrato_id = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "sistema"},
        files={"arquivo": ("banco.ofx", OFX, "application/x-ofx")},
    ).json()["extrato_id"]

    for resposta in (_revisar(c.headers, extrato_id), _confirmar(c.headers, extrato_id)):
        assert (resposta.status_code, resposta.json()) == (404, NAO_ENCONTRADA)


# --- vencido, outra empresa, outra conta ----------------------------------------------


def test_rascunho_vencido_no_meio_da_revisao_e_404_em_tudo(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    linha = _ids(c.headers, importacao_id)[0]
    _envelhecer(db_session, importacao_id, 7.01)

    respostas = [
        _revisar(c.headers, importacao_id),
        _corrigir(c.headers, importacao_id, linha, valor="1.00"),
        _remover(c.headers, importacao_id, linha),
        _adicionar(c.headers, importacao_id, **LANCAMENTOS[0]),
        _confirmar(c.headers, importacao_id),
    ]

    assert [(r.status_code, r.json()) for r in respostas] == [(404, NAO_ENCONTRADA)] * 5


def test_outra_empresa_e_outra_conta_sao_404(db_session, conta):
    c = conta(outra=True)
    outra_conta = conta()
    importacao_id = _importar(c.headers)
    linha = _ids(c.headers, importacao_id)[0]

    for headers in (c.headers_outra, outra_conta.headers):
        respostas = [
            _revisar(headers, importacao_id),
            _corrigir(headers, importacao_id, linha, valor="1.00"),
            _remover(headers, importacao_id, linha),
            _adicionar(headers, importacao_id, **LANCAMENTOS[0]),
            _confirmar(headers, importacao_id),
        ]
        assert [(r.status_code, r.json()) for r in respostas] == [(404, NAO_ENCONTRADA)] * 5
    com_a_empresa_de_outra_conta = _revisar(
        {**outra_conta.headers, CABECALHO_EMPRESA: str(c.empresa_id)}, importacao_id
    )
    assert com_a_empresa_de_outra_conta.json() == {"detail": "Empresa não encontrada."}
    assert _revisar(c.headers, importacao_id).json()["total"] == 3


def test_linha_de_outro_rascunho_e_404(db_session, conta):
    c = conta()
    um = _importar(c.headers)
    outro = _importar(c.headers)
    linha_do_outro = _ids(c.headers, outro)[0]

    for resposta in (
        _corrigir(c.headers, um, linha_do_outro, valor="1.00"),
        _remover(c.headers, um, linha_do_outro),
    ):
        assert (resposta.status_code, resposta.json()) == (
            404,
            {"detail": "Lançamento não encontrado."},
        )
    assert _revisar(c.headers, outro).json()["total"] == 3


def test_escritas_tem_limite_por_usuario(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    linha = _ids(c.headers, importacao_id)[0]

    respostas = [
        _corrigir(c.headers, importacao_id, linha, descricao=f"Aluguel {i}").status_code
        for i in range(61)
    ]

    assert respostas == [200] * 60 + [429]


# --- concorrência ---------------------------------------------------------------------


def _segurar_o_rascunho(importacao_id):
    """Trava a linha do extrato numa conexão à parte, como uma confirmação em
    andamento, e devolve a função que solta."""
    conexao = engine.connect()
    transacao = conexao.begin()
    conexao.execute(
        text("SELECT id FROM extratos WHERE id = :id FOR UPDATE"), {"id": importacao_id}
    )

    def soltar():
        transacao.commit()
        conexao.close()

    return soltar


def _ao_mesmo_tempo(importacao_id, *funcoes):
    soltar = _segurar_o_rascunho(importacao_id)
    resultados: list = [None] * len(funcoes)

    def rodar(i, funcao):
        resultados[i] = funcao()

    threads = [threading.Thread(target=rodar, args=(i, f)) for i, f in enumerate(funcoes)]
    try:
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        assert all(thread.is_alive() for thread in threads), "deveria esperar a trava"
    finally:
        soltar()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return resultados


def test_duas_confirmacoes_ao_mesmo_tempo(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    respostas = _ao_mesmo_tempo(
        importacao_id,
        lambda: _confirmar(c.headers, importacao_id),
        lambda: _confirmar(c.headers, importacao_id),
    )

    assert sorted(r.status_code for r in respostas) == [200, 409]
    assert [r.json() for r in respostas if r.status_code == 409] == [CONFIRMADA]
    assert len(_linhas(db_session, importacao_id)) == 3


def test_descarte_e_confirmacao_ao_mesmo_tempo(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)

    confirmar, descartar = _ao_mesmo_tempo(
        importacao_id,
        lambda: _confirmar(c.headers, importacao_id),
        lambda: client.delete(f"/importacoes-ia/{importacao_id}", headers=c.headers),
    )

    db_session.expire_all()
    extrato = db_session.get(Extrato, uuid.UUID(importacao_id))
    if confirmar.status_code == 200:
        # Confirmou primeiro: o descarte não acha mais rascunho, e o extrato fica inteiro.
        assert (descartar.status_code, descartar.json()) == (404, NAO_ENCONTRADA)
        assert extrato.status == "concluido"
        assert len(_linhas(db_session, importacao_id)) == 3
    else:
        # Descartou primeiro: a confirmação não acha nada, e nada sobra.
        assert descartar.status_code == 204
        assert (confirmar.status_code, confirmar.json()) == (404, NAO_ENCONTRADA)
        assert extrato is None
        assert _linhas(db_session, importacao_id) == []


def test_correcao_espera_a_confirmacao_e_recebe_409(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers)
    linha = _ids(c.headers, importacao_id)[0]
    # Uma "confirmação" em andamento: trava a linha e muda o status na mesma transação.
    conexao = engine.connect()
    transacao = conexao.begin()
    conexao.execute(
        text("SELECT id FROM extratos WHERE id = :id FOR UPDATE"), {"id": importacao_id}
    )
    conexao.execute(
        text("UPDATE extratos SET status = 'concluido' WHERE id = :id"), {"id": importacao_id}
    )
    resultado: dict = {}
    thread = threading.Thread(
        target=lambda: resultado.update(r=_corrigir(c.headers, importacao_id, linha, valor="1.00"))
    )
    try:
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "deveria esperar a trava"
    finally:
        transacao.commit()
        conexao.close()
    thread.join(timeout=20)

    assert (resultado["r"].status_code, resultado["r"].json()) == (409, CONFIRMADA)
    assert str(_linhas(db_session, importacao_id)[0].valor) == "-100.00"
