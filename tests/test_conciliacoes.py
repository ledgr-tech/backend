"""Testes do POST /conciliacoes (issue #16, ADR-006 e ADR-007) contra Postgres
real, mais o teste ponta a ponta do critério de aceite: par sintético gerado
por scripts/gerar_extratos_sinteticos.py --par, enviado via upload.

O TestClient roda as BackgroundTasks antes de devolver a resposta (ver
tests/test_extratos_upload_normalizacao.py), então os extratos enviados pelo
upload já estão processados quando o teste continua.
"""

import hashlib
import io
import re
import threading
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert

from app.core.database import SessionLocal, engine
from app.models import Conciliacao, Extrato, Lancamento
from app.services import matching
from main import app
from scripts.gerar_extratos_sinteticos import main as gerar_extratos

client = TestClient(app)


@pytest.fixture
def criar_extrato_da_empresa(db_session):
    """Extrato com status/origem à escolha numa empresa já existente (a
    fábrica `criar_extrato` do conftest cria uma empresa nova a cada chamada)."""

    def _criar(empresa_id, origem: str, status: str = "concluido") -> Extrato:
        extrato = Extrato(
            empresa_id=empresa_id,
            nome_arquivo="extrato.csv",
            formato="csv",
            tamanho_bytes=0,
            origem=origem,
            status=status,
        )
        db_session.add(extrato)
        db_session.commit()
        return extrato

    return _criar


@pytest.fixture
def inserir_lancamentos(db_session):
    def _inserir(extrato: Extrato, itens: list[tuple[str, str]]) -> list[Lancamento]:
        """itens: (valor, descricao), sempre na mesma data. Cada item repetido
        recebe a próxima ocorrência, como faz a normalização (ADR-006)."""
        vistos: dict[tuple[str, str], int] = {}
        criados = []
        for valor, descricao in itens:
            vistos[(valor, descricao)] = vistos.get((valor, descricao), 0) + 1
            lancamento = Lancamento(
                empresa_id=extrato.empresa_id,
                extrato_id=extrato.id,
                data=date(2026, 9, 5),
                valor=Decimal(valor),
                descricao=descricao,
                tipo="credito" if Decimal(valor) > 0 else "debito",
                hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                ocorrencia=vistos[(valor, descricao)],
            )
            db_session.add(lancamento)
            criados.append(lancamento)
        db_session.commit()
        return criados

    return _inserir


def _post(headers, banco_id, sistema_id):
    return client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )


def _conciliacoes(db_session, banco_id, sistema_id):
    db_session.expire_all()
    return (
        db_session.query(Conciliacao)
        .filter_by(extrato_banco_id=banco_id, extrato_sistema_id=sistema_id)
        .all()
    )


def test_post_feliz_grava_linhas_e_devolve_contagens(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(
        banco, [("10.00", "a"), ("20.00", "b"), ("150.00", "c"), ("150.00", "c"), ("150.00", "c")]
    )
    inserir_lancamentos(sistema, [("10.00", "a"), ("20.00", "b"), ("150.00", "c"), ("150.00", "c")])
    inserir_lancamentos(sistema, [("77.00", "so sistema")])

    response = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert response.status_code == 201
    assert response.json() == {
        "extrato_banco_id": str(banco.id),
        "extrato_sistema_id": str(sistema.id),
        "total": 6,
        "match_exato": 4,
        "duplicado": 1,
        "sem_correspondencia": 1,
    }
    linhas = _conciliacoes(db_session, banco.id, sistema.id)
    assert len(linhas) == 6
    assert all(linha.empresa_id == empresa.id for linha in linhas)
    matches = [linha for linha in linhas if linha.status == "match_exato"]
    assert len(matches) == 4
    for match in matches:
        assert match.regra_aplicada == "exato"
        assert match.score_confianca == Decimal("1.000")
        assert match.lancamento_banco_id is not None
        assert match.lancamento_sistema_id is not None
    for outra in (linha for linha in linhas if linha.status != "match_exato"):
        assert outra.regra_aplicada is None
        assert outra.score_confianca is None
        assert (outra.lancamento_banco_id is None) != (outra.lancamento_sistema_id is None)


def test_post_repetido_e_idempotente(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b")])
    inserir_lancamentos(sistema, [("10.00", "a")])

    primeira = _post(auth_headers(empresa.id), banco.id, sistema.id)
    ids_primeira = {linha.id for linha in _conciliacoes(db_session, banco.id, sistema.id)}
    segunda = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert primeira.status_code == segunda.status_code == 201
    assert primeira.json() == segunda.json()
    linhas = _conciliacoes(db_session, banco.id, sistema.id)
    assert len(linhas) == 2
    assert not ids_primeira & {linha.id for linha in linhas}  # substituídas, não somadas


def test_extrato_de_outra_empresa_retorna_404(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    outra = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")
    sistema = criar_extrato_da_empresa(dona.id, "sistema")

    response = _post(auth_headers(outra.id), banco.id, sistema.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Extrato não encontrado."


def test_extrato_inexistente_retorna_404(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _post(auth_headers(empresa.id), banco.id, uuid.uuid4()).status_code == 404


def test_origem_trocada_retorna_422(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _post(auth_headers(empresa.id), sistema.id, banco.id).status_code == 422


def test_mesmo_id_nos_dois_lados_retorna_422(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _post(auth_headers(empresa.id), banco.id, banco.id).status_code == 422


@pytest.mark.parametrize("status", ["pendente", "processando", "erro"])
def test_extrato_nao_processado_retorna_409(
    status, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", status=status)
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _post(auth_headers(empresa.id), banco.id, sistema.id).status_code == 409


def test_extrato_concluido_com_erros_e_aceito(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", status="concluido_com_erros")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    response = _post(auth_headers(empresa.id), banco.id, sistema.id)

    assert response.status_code == 201
    assert response.json()["total"] == 0


def test_sem_token_retorna_401():
    response = client.post(
        "/conciliacoes",
        json={"extrato_banco_id": str(uuid.uuid4()), "extrato_sistema_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401


def test_par_sintetico_via_upload_gera_o_numero_esperado_de_matches(
    db_session, criar_empresa, auth_headers, tmp_path, capsys
):
    quantidade, sobreposicao = 40, 25
    gerar_extratos(
        [
            "--formato", "csv", "--par",
            "--quantidade", str(quantidade),
            "--sobreposicao", str(sobreposicao),
            "--seed", "16",
            "--saida", str(tmp_path),
        ]
    )  # fmt: skip
    esperado = int(re.search(r"matches_esperados=(\d+)", capsys.readouterr().out).group(1))
    assert esperado == sobreposicao

    empresa = criar_empresa()
    ids = {}
    for origem in ("banco", "sistema"):
        arquivo = next(tmp_path.glob(f"extrato_par_{origem}_*.csv"))
        response = client.post(
            "/extratos/upload",
            headers=auth_headers(empresa.id),
            data={"origem": origem},
            files={"arquivo": (arquivo.name, io.BytesIO(arquivo.read_bytes()), "text/csv")},
        )
        assert response.status_code == 201
        ids[origem] = response.json()["extrato_id"]
        db_session.expire_all()
        extrato = db_session.get(Extrato, uuid.UUID(ids[origem]))
        assert extrato.status == "concluido"
        assert extrato.quantidade_lancamentos == quantidade

    response = _post(auth_headers(empresa.id), ids["banco"], ids["sistema"])

    assert response.status_code == 201
    corpo = response.json()
    assert corpo["match_exato"] == esperado
    assert corpo["duplicado"] == 0
    assert corpo["sem_correspondencia"] == 2 * (quantidade - sobreposicao)
    assert corpo["total"] == esperado + 2 * (quantidade - sobreposicao)


def test_duas_conciliacoes_simultaneas_do_mesmo_par_nao_duplicam_linhas(
    db_session,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    monkeypatch,
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    inserir_lancamentos(sistema, [("10.00", "a"), ("20.00", "b")])

    # Ids como valores simples: atributos ORM da sessão principal não podem
    # ser lidos de dentro das threads.
    empresa_id, banco_id, sistema_id = empresa.id, banco.id, sistema.id

    esperado = matching.conciliar_extratos(db_session, empresa_id, banco_id, sistema_id)["total"]
    assert esperado == 3

    # Alarga a janela de corrida entre o delete e o insert: sem o lock, as duas
    # threads apagam o que já existia, esperam e inserem, dobrando as linhas
    # (o delete da segunda não enxerga o que a primeira ainda vai inserir).
    insert_original = matching.insert

    def insert_lento(*args, **kwargs):
        time.sleep(1.5)
        return insert_original(*args, **kwargs)

    monkeypatch.setattr(matching, "insert", insert_lento)

    largada = threading.Barrier(2)
    erros: list[BaseException] = []

    def conciliar_em_sessao_propria():
        sessao = SessionLocal()
        try:
            largada.wait(timeout=30)
            matching.conciliar_extratos(sessao, empresa_id, banco_id, sistema_id)
        except BaseException as exc:  # noqa: BLE001 - reporta no thread principal
            erros.append(exc)
        finally:
            sessao.close()

    threads = [threading.Thread(target=conciliar_em_sessao_propria) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert erros == []
    assert len(_conciliacoes(db_session, banco_id, sistema_id)) == esperado


def test_insert_em_lote_emite_poucos_statements_mesmo_com_padroes_intercalados(
    db_session, criar_empresa, criar_extrato_da_empresa
):
    """Regressão pra um statement por bloco ou por linha, sem depender de tempo.

    O bulk insert do ORM (`insert(Conciliacao)`) quebra o lote a cada mudança
    no padrão de campos nulos (par casado, só banco, só sistema), e cada
    statement custa uma ida e volta ao banco. Aqui os resultados saem
    intercalados (par, só banco, par, só sistema...), o pior padrão: pelo ORM
    seriam milhares de statements. Pelo Core tem que ser um número pequeno e
    que não cresce com as trocas de padrão.
    """
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    empresa_id, banco_id, sistema_id = empresa.id, banco.id, sistema.id

    def linha(extrato_id, indice):
        return {
            "id": uuid.uuid4(),
            "empresa_id": empresa_id,
            "extrato_id": extrato_id,
            "data": date(2026, 9, 5),
            "valor": Decimal(indice + 1) / 100,  # valor único por índice: sem grupos
            "descricao": f"Lancamento {indice}",
            "tipo": "credito",
            "hash_dedup": hashlib.sha256(f"{extrato_id}|{indice}".encode()).hexdigest(),
            "ocorrencia": 1,
        }

    quantidade = 4_000
    linhas_banco, linhas_sistema = [], []
    for indice in range(quantidade):
        # padrão por índice: 0 par, 1 só banco, 2 par, 3 só sistema
        if indice % 4 in (0, 2, 1):
            linhas_banco.append(linha(banco_id, indice))
        if indice % 4 in (0, 2, 3):
            linhas_sistema.append(linha(sistema_id, indice))
    db_session.execute(insert(Lancamento.__table__), linhas_banco + linhas_sistema)
    db_session.commit()

    statements: list[str] = []

    def contar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO CONCILIACOES"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        contagens = matching.conciliar_extratos(db_session, empresa_id, banco_id, sistema_id)
    finally:
        event.remove(engine, "before_cursor_execute", contar)

    assert contagens["match_exato"] == quantidade // 2
    assert contagens["total"] == quantidade
    assert contagens["sem_correspondencia"] == quantidade // 2
    assert len(statements) <= 5, f"{len(statements)} statements de INSERT em conciliacoes"


# GET /conciliacoes/{extrato_id} (issue #20, ADR-007)


def _get(headers, extrato_id, **params):
    return client.get(f"/conciliacoes/{extrato_id}", headers=headers, params=params)


def _par_conciliado(empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers, itens):
    """Cria extrato banco e sistema, insere os lançamentos e roda o POST.
    `itens` = (lista do banco, lista do sistema), cada uma de (valor, descricao)."""
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, itens[0])
    inserir_lancamentos(sistema, itens[1])
    assert _post(auth_headers(empresa.id), banco.id, sistema.id).status_code == 201
    return banco, sistema


def test_get_devolve_conciliacoes_com_lancamentos_e_pontas_nulas(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco, sistema = _par_conciliado(
        empresa,
        criar_extrato_da_empresa,
        inserir_lancamentos,
        auth_headers,
        (
            [("10.00", "a"), ("20.00", "b"), ("150.00", "so banco")],
            [("10.00", "a"), ("20.00", "b"), ("77.00", "so sistema")],
        ),
    )

    response = _get(auth_headers(empresa.id), banco.id)

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["extrato_id"] == str(banco.id)
    assert (corpo["total"], corpo["limit"], corpo["offset"]) == (4, 100, 0)
    # ordem: data, valor (do lançamento do banco ou, sem ele, do sistema), id
    assert [item["status"] for item in corpo["itens"]] == [
        "match_exato",
        "match_exato",
        "sem_correspondencia",
        "sem_correspondencia",
    ]
    primeiro, _, so_sistema, so_banco = corpo["itens"]
    assert primeiro["extrato_sistema_id"] == str(sistema.id)
    assert primeiro["regra_aplicada"] == "exato"
    assert primeiro["score_confianca"] == "1.000"  # Decimal como string
    assert primeiro["lancamento_banco"]["valor"] == "10.00"
    assert primeiro["lancamento_banco"]["descricao"] == "a"
    assert primeiro["lancamento_banco"]["data"] == "2026-09-05"
    assert primeiro["lancamento_banco"]["tipo"] == "credito"
    assert set(primeiro["lancamento_banco"]) == {"id", "data", "valor", "descricao", "tipo"}
    assert primeiro["lancamento_sistema"]["valor"] == "10.00"
    assert so_sistema["lancamento_banco"] is None
    assert so_sistema["lancamento_sistema"]["descricao"] == "so sistema"
    assert so_sistema["regra_aplicada"] is None and so_sistema["score_confianca"] is None
    assert so_banco["lancamento_sistema"] is None
    assert so_banco["lancamento_banco"]["descricao"] == "so banco"


def test_get_extrato_sem_conciliacao_devolve_lista_vazia(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    response = _get(auth_headers(empresa.id), banco.id)

    assert response.status_code == 200
    assert response.json() == {
        "extrato_id": str(banco.id),
        "total": 0,
        "limit": 100,
        "offset": 0,
        "itens": [],
    }


def test_get_paginacao_nao_repete_nem_perde_itens(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    valores = [(f"{indice + 1}.00", f"l{indice}") for indice in range(25)]
    banco, _ = _par_conciliado(
        empresa,
        criar_extrato_da_empresa,
        inserir_lancamentos,
        auth_headers,
        (valores, valores[:15]),  # 15 pares e 10 só do banco
    )
    headers = auth_headers(empresa.id)

    tudo = _get(headers, banco.id, limit=1000).json()
    paginas = [_get(headers, banco.id, limit=10, offset=offset).json() for offset in (0, 10, 20)]

    assert tudo["total"] == 25 and len(tudo["itens"]) == 25
    assert [len(pagina["itens"]) for pagina in paginas] == [10, 10, 5]
    assert all(pagina["total"] == 25 and pagina["limit"] == 10 for pagina in paginas)
    ids_paginados = [item["id"] for pagina in paginas for item in pagina["itens"]]
    assert len(set(ids_paginados)) == 25
    assert ids_paginados == [item["id"] for item in tudo["itens"]]  # mesma ordem estável
    assert _get(headers, banco.id, limit=10, offset=25).json()["itens"] == []


def test_get_filtra_por_status(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco, _ = _par_conciliado(
        empresa,
        criar_extrato_da_empresa,
        inserir_lancamentos,
        auth_headers,
        ([("1.00", "a"), ("2.00", "b"), ("3.00", "c")], [("1.00", "a"), ("2.00", "b")]),
    )

    matches = _get(auth_headers(empresa.id), banco.id, status="match_exato").json()
    sem_par = _get(auth_headers(empresa.id), banco.id, status="sem_correspondencia").json()
    duplicados = _get(auth_headers(empresa.id), banco.id, status="duplicado").json()

    assert matches["total"] == 2 and {i["status"] for i in matches["itens"]} == {"match_exato"}
    assert sem_par["total"] == 1
    assert duplicados["total"] == 0 and duplicados["itens"] == []


def test_get_filtra_por_extrato_sistema_id_de_pares_diferentes(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema_1 = criar_extrato_da_empresa(empresa.id, "sistema")
    sistema_2 = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("1.00", "a"), ("2.00", "b")])
    inserir_lancamentos(sistema_1, [("1.00", "a")])
    inserir_lancamentos(sistema_2, [("2.00", "b"), ("3.00", "c")])
    headers = auth_headers(empresa.id)
    assert _post(headers, banco.id, sistema_1.id).status_code == 201
    assert _post(headers, banco.id, sistema_2.id).status_code == 201

    todos = _get(headers, banco.id).json()
    do_par_1 = _get(headers, banco.id, extrato_sistema_id=str(sistema_1.id)).json()
    do_par_2 = _get(headers, banco.id, extrato_sistema_id=str(sistema_2.id)).json()

    assert todos["total"] == 5  # par 1: 2 linhas, par 2: 3 linhas
    assert do_par_1["total"] == 2
    assert do_par_2["total"] == 3
    assert {i["extrato_sistema_id"] for i in do_par_1["itens"]} == {str(sistema_1.id)}
    assert {i["extrato_sistema_id"] for i in do_par_2["itens"]} == {str(sistema_2.id)}


def test_get_extrato_de_outra_empresa_e_inexistente_retornam_404(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    outra = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")

    de_outra = _get(auth_headers(outra.id), banco.id)
    inexistente = _get(auth_headers(dona.id), uuid.uuid4())

    assert de_outra.status_code == inexistente.status_code == 404
    assert de_outra.json()["detail"] == "Extrato não encontrado."


def test_get_extrato_de_origem_sistema_retorna_422(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _get(auth_headers(empresa.id), sistema.id).status_code == 422


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"status": "inexistente"}],
    ids=["limit_0", "limit_1001", "offset_negativo", "status_invalido"],
)
def test_get_parametros_invalidos_retornam_422(
    params, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _get(auth_headers(empresa.id), banco.id, **params).status_code == 422


def test_get_sem_token_retorna_401():
    assert client.get(f"/conciliacoes/{uuid.uuid4()}").status_code == 401


def test_get_emite_poucos_selects_e_nao_cresce_com_o_tamanho_da_pagina(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    valores = [(f"{indice + 1}.00", f"l{indice}") for indice in range(30)]
    banco, _ = _par_conciliado(
        empresa,
        criar_extrato_da_empresa,
        inserir_lancamentos,
        auth_headers,
        (valores, valores[:20]),
    )

    def selects_da_chamada(limit):
        selects: list[str] = []

        def contar(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(engine, "before_cursor_execute", contar)
        try:
            response = _get(auth_headers(empresa.id), banco.id, limit=limit)
        finally:
            event.remove(engine, "before_cursor_execute", contar)
        assert response.status_code == 200
        assert len(response.json()["itens"]) == limit
        return len(selects)

    pequena, cheia = selects_da_chamada(5), selects_da_chamada(30)

    # validação do extrato, query dos itens (com join) e contagem: sem N+1
    assert pequena <= 4 and cheia <= 4
    assert pequena == cheia


def test_get_apos_par_sintetico_soma_das_paginas_bate_com_o_total_do_post(
    criar_empresa, auth_headers, tmp_path
):
    quantidade, sobreposicao = 40, 25
    gerar_extratos(
        [
            "--formato", "csv", "--par",
            "--quantidade", str(quantidade),
            "--sobreposicao", str(sobreposicao),
            "--seed", "20",
            "--saida", str(tmp_path),
        ]
    )  # fmt: skip
    empresa = criar_empresa()
    ids = {}
    for origem in ("banco", "sistema"):
        arquivo = next(tmp_path.glob(f"extrato_par_{origem}_*.csv"))
        response = client.post(
            "/extratos/upload",
            headers=auth_headers(empresa.id),
            data={"origem": origem},
            files={"arquivo": (arquivo.name, io.BytesIO(arquivo.read_bytes()), "text/csv")},
        )
        assert response.status_code == 201
        ids[origem] = response.json()["extrato_id"]
    post = _post(auth_headers(empresa.id), ids["banco"], ids["sistema"]).json()

    itens, offset = [], 0
    while True:
        pagina = _get(auth_headers(empresa.id), ids["banco"], limit=10, offset=offset).json()
        assert pagina["total"] == post["total"]
        itens += pagina["itens"]
        if len(pagina["itens"]) < 10:
            break
        offset += 10

    assert len(itens) == post["total"]
    assert len({item["id"] for item in itens}) == post["total"]
    assert sum(item["status"] == "match_exato" for item in itens) == post["match_exato"]
    assert post["match_exato"] == sobreposicao
