"""Testes da issue #27 (ADR-010): função pura `calcular_percentual_acerto`
(app/services/execucoes.py) e o endpoint `GET /execucoes`
(app/api/execucoes.py).

O teste de que `POST /conciliacoes` grava a execução (com `match_tolerancia`
certo no response, e a regressão do bug de soma com tolerância > 0) fica em
tests/test_conciliacoes.py, junto dos outros testes do POST.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Extrato, Lancamento
from app.services.execucoes import calcular_percentual_acerto
from main import app

client = TestClient(app)


# calcular_percentual_acerto (função pura, sem banco)


def test_percentual_arredonda_meio_para_cima():
    # 2/3 = 66.666...% -> 66.67
    assert calcular_percentual_acerto(match_exato=2, match_tolerancia=0, total=3) == Decimal(
        "66.67"
    )


def test_percentual_soma_match_exato_e_match_tolerancia():
    assert calcular_percentual_acerto(match_exato=1, match_tolerancia=1, total=4) == Decimal(
        "50.00"
    )


def test_percentual_total_zero_e_none():
    assert calcular_percentual_acerto(match_exato=0, match_tolerancia=0, total=0) is None


def test_percentual_100_por_cento():
    assert calcular_percentual_acerto(match_exato=3, match_tolerancia=0, total=3) == Decimal(
        "100.00"
    )


def test_percentual_0_por_cento():
    assert calcular_percentual_acerto(match_exato=0, match_tolerancia=0, total=5) == Decimal("0.00")


# GET /execucoes


def _get(headers, **params):
    return client.get("/execucoes", headers=headers, params=params)


@pytest.fixture
def criar_extrato_da_empresa(db_session):
    def _criar(
        empresa_id,
        origem: str,
        nome_arquivo: str = "extrato.csv",
        periodo_inicio: date | None = None,
        periodo_fim: date | None = None,
    ) -> Extrato:
        extrato = Extrato(
            empresa_id=empresa_id,
            nome_arquivo=nome_arquivo,
            formato="csv",
            tamanho_bytes=0,
            origem=origem,
            status="concluido",
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
        )
        db_session.add(extrato)
        db_session.commit()
        return extrato

    return _criar


def _post_conciliacao(auth_headers, empresa_id, banco_id, sistema_id):
    return client.post(
        "/conciliacoes",
        headers=auth_headers(empresa_id),
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )


@pytest.fixture
def inserir_lancamentos(db_session):
    def _inserir(extrato: Extrato, itens: list[tuple[str, str]], data: date) -> None:
        for valor, descricao in itens:
            db_session.add(
                Lancamento(
                    empresa_id=extrato.empresa_id,
                    extrato_id=extrato.id,
                    data=data,
                    valor=Decimal(valor),
                    descricao=descricao,
                    tipo="credito" if Decimal(valor) > 0 else "debito",
                    hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    ocorrencia=1,
                )
            )
        db_session.commit()

    return _inserir


def test_isolamento_entre_empresas(
    criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa_a = criar_empresa()
    empresa_b = criar_empresa()
    banco_a = criar_extrato_da_empresa(empresa_a.id, "banco")
    sistema_a = criar_extrato_da_empresa(empresa_a.id, "sistema")
    banco_b = criar_extrato_da_empresa(empresa_b.id, "banco")
    sistema_b = criar_extrato_da_empresa(empresa_b.id, "sistema")
    inserir_lancamentos(banco_a, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(sistema_a, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(banco_b, [("20.00", "b")], date(2026, 9, 5))
    inserir_lancamentos(sistema_b, [("20.00", "b")], date(2026, 9, 5))
    assert (
        _post_conciliacao(auth_headers, empresa_a.id, banco_a.id, sistema_a.id).status_code == 201
    )
    assert (
        _post_conciliacao(auth_headers, empresa_b.id, banco_b.id, sistema_b.id).status_code == 201
    )

    resposta_a = _get(auth_headers(empresa_a.id))
    resposta_b = _get(auth_headers(empresa_b.id))

    assert resposta_a.status_code == resposta_b.status_code == 200
    corpo_a, corpo_b = resposta_a.json(), resposta_b.json()
    assert corpo_a["total"] == 1 and corpo_b["total"] == 1
    assert corpo_a["itens"][0]["extrato_banco_id"] == str(banco_a.id)
    assert corpo_b["itens"][0]["extrato_banco_id"] == str(banco_b.id)


def test_empresa_sem_execucao_devolve_lista_vazia(criar_empresa, auth_headers):
    empresa = criar_empresa()

    response = _get(auth_headers(empresa.id))

    assert response.status_code == 200
    assert response.json() == {"total": 0, "limit": 20, "offset": 0, "itens": []}


def test_sem_token_retorna_401():
    assert client.get("/execucoes").status_code == 401


def test_ordem_mais_recente_primeiro_e_paginacao(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    pares = []
    for indice in range(3):
        banco = criar_extrato_da_empresa(empresa.id, "banco", nome_arquivo=f"banco_{indice}.csv")
        sistema = criar_extrato_da_empresa(
            empresa.id, "sistema", nome_arquivo=f"sistema_{indice}.csv"
        )
        inserir_lancamentos(banco, [(f"{indice + 1}.00", "a")], date(2026, 9, 5))
        inserir_lancamentos(sistema, [(f"{indice + 1}.00", "a")], date(2026, 9, 5))
        assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201
        pares.append((banco, sistema))

    tudo = _get(headers, limit=100).json()
    pagina_1 = _get(headers, limit=2, offset=0).json()
    pagina_2 = _get(headers, limit=2, offset=2).json()

    assert tudo["total"] == 3
    # mais recente primeiro: o último par criado (índice 2) vem primeiro.
    assert [item["extrato_banco_id"] for item in tudo["itens"]] == [
        str(pares[2][0].id),
        str(pares[1][0].id),
        str(pares[0][0].id),
    ]
    assert len(pagina_1["itens"]) == 2 and len(pagina_2["itens"]) == 1
    assert pagina_1["itens"] + pagina_2["itens"] == tudo["itens"]
    for item in tudo["itens"]:
        assert item["atual"] is True  # cada par só foi conciliado uma vez


def test_percentual_acerto_no_json_e_null_quando_total_zero(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    item = _get(auth_headers(empresa.id)).json()["itens"][0]

    assert item["percentual_acerto"] is None
    assert item["contagens"]["total"] == 0
    assert item["nome_arquivo_banco"] == banco.nome_arquivo
    assert item["nome_arquivo_sistema"] == sistema.nome_arquivo


def test_executada_em_sai_em_utc_explicito(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    executada_em_str = _get(auth_headers(empresa.id)).json()["itens"][0]["executada_em"]

    assert executada_em_str.endswith(("Z", "+00:00"))
    executada_em = datetime.fromisoformat(executada_em_str)
    assert executada_em.tzinfo is not None
    assert abs(datetime.now(UTC) - executada_em) < timedelta(seconds=10)


def test_reconciliar_o_mesmo_par_gera_2_execucoes_e_atual_so_na_mais_recente(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(sistema, [("10.00", "a")], date(2026, 9, 5))
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201
    # muda o par: agora nenhum lançamento casa, pra segunda execução ter
    # contagens diferentes da primeira.
    inserir_lancamentos(banco, [("77.00", "novo")], date(2026, 9, 6))
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    linhas_conciliacoes = (
        db_session.query(Conciliacao)
        .filter_by(extrato_banco_id=banco.id, extrato_sistema_id=sistema.id)
        .all()
    )

    resposta = _get(auth_headers(empresa.id))
    itens = resposta.json()["itens"]

    assert resposta.json()["total"] == 2
    assert len(itens) == 2
    # conciliacoes guarda só a última rodada (ADR-007), sem duplicar linhas.
    assert len(linhas_conciliacoes) == 2  # match_exato + sem_correspondencia da 2a rodada
    atuais = [item["atual"] for item in itens]
    assert atuais.count(True) == 1
    assert itens[atuais.index(True)]["id"] == itens[0]["id"]  # a mais recente é a primeira da lista


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}],
    ids=["limit_0", "limit_101", "offset_negativo"],
)
def test_parametros_invalidos_retornam_422(params, criar_empresa, auth_headers):
    empresa = criar_empresa()

    assert _get(auth_headers(empresa.id), **params).status_code == 422


def test_configuracao_com_tolerancia_grava_tolerancia_dias_na_execucao(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    db_session.add(Configuracao(empresa_id=empresa.id, tolerancia_dias_default=2))
    db_session.commit()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(sistema, [("10.00", "a")], date(2026, 9, 6))
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    item = _get(auth_headers(empresa.id)).json()["itens"][0]

    assert item["tolerancia_dias"] == 2
    assert item["contagens"]["match_tolerancia"] == 1
    assert item["percentual_acerto"] == "100.00"


# filtro ?extrato_banco_id= e período (issue #82)


def test_filtro_extrato_banco_id_devolve_so_as_execucoes_daquele_extrato(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco_1 = criar_extrato_da_empresa(empresa.id, "banco", nome_arquivo="banco_1.csv")
    sistema_1 = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="sistema_1.csv")
    banco_2 = criar_extrato_da_empresa(empresa.id, "banco", nome_arquivo="banco_2.csv")
    sistema_2 = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="sistema_2.csv")
    inserir_lancamentos(banco_1, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(sistema_1, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(banco_2, [("20.00", "b")], date(2026, 9, 6))
    inserir_lancamentos(sistema_2, [("20.00", "b")], date(2026, 9, 6))
    assert _post_conciliacao(auth_headers, empresa.id, banco_1.id, sistema_1.id).status_code == 201
    assert _post_conciliacao(auth_headers, empresa.id, banco_2.id, sistema_2.id).status_code == 201

    resposta = _get(auth_headers(empresa.id), extrato_banco_id=str(banco_1.id))

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["total"] == 1
    assert len(corpo["itens"]) == 1
    assert corpo["itens"][0]["extrato_banco_id"] == str(banco_1.id)


def test_filtro_total_respeita_o_filtro_com_limit_e_offset(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    for indice in range(3):
        sistema = criar_extrato_da_empresa(
            empresa.id, "sistema", nome_arquivo=f"sistema_{indice}.csv"
        )
        inserir_lancamentos(banco, [(f"{indice + 1}.00", f"a{indice}")], date(2026, 9, 5))
        inserir_lancamentos(sistema, [(f"{indice + 1}.00", f"a{indice}")], date(2026, 9, 5))
        assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201
    # execução de outro extrato do banco, na mesma empresa — não deve contar no filtro.
    outro_banco = criar_extrato_da_empresa(empresa.id, "banco", nome_arquivo="outro_banco.csv")
    outro_sistema = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="outro_s.csv")
    inserir_lancamentos(outro_banco, [("1.00", "x")], date(2026, 9, 1))
    inserir_lancamentos(outro_sistema, [("1.00", "x")], date(2026, 9, 1))
    assert (
        _post_conciliacao(auth_headers, empresa.id, outro_banco.id, outro_sistema.id).status_code
        == 201
    )

    pagina_1 = _get(auth_headers(empresa.id), extrato_banco_id=str(banco.id), limit=2, offset=0)
    pagina_2 = _get(auth_headers(empresa.id), extrato_banco_id=str(banco.id), limit=2, offset=2)

    assert pagina_1.json()["total"] == 3
    assert pagina_2.json()["total"] == 3
    assert len(pagina_1.json()["itens"]) == 2
    assert len(pagina_2.json()["itens"]) == 1
    assert all(
        item["extrato_banco_id"] == str(banco.id)
        for item in pagina_1.json()["itens"] + pagina_2.json()["itens"]
    )


def test_filtro_atual_correto_quando_o_mesmo_par_e_reconciliado(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    outro_banco = criar_extrato_da_empresa(empresa.id, "banco", nome_arquivo="outro.csv")
    outro_sistema = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="outro_s.csv")
    inserir_lancamentos(outro_banco, [("1.00", "x")], date(2026, 9, 1))
    inserir_lancamentos(outro_sistema, [("1.00", "x")], date(2026, 9, 1))
    assert (
        _post_conciliacao(auth_headers, empresa.id, outro_banco.id, outro_sistema.id).status_code
        == 201
    )
    inserir_lancamentos(banco, [("10.00", "a")], date(2026, 9, 5))
    inserir_lancamentos(sistema, [("10.00", "a")], date(2026, 9, 5))
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201
    inserir_lancamentos(banco, [("77.00", "novo")], date(2026, 9, 6))
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    resposta = _get(auth_headers(empresa.id), extrato_banco_id=str(banco.id))
    itens = resposta.json()["itens"]

    assert resposta.json()["total"] == 2
    assert len(itens) == 2
    atuais = [item["atual"] for item in itens]
    assert atuais.count(True) == 1
    assert itens[atuais.index(True)]["id"] == itens[0]["id"]  # mais recente primeiro


def test_filtro_com_uuid_invalido_da_422(criar_empresa, auth_headers):
    empresa = criar_empresa()

    resposta = _get(auth_headers(empresa.id), extrato_banco_id="nao-e-um-uuid")

    assert resposta.status_code == 422


def test_filtro_com_uuid_inexistente_da_404(criar_empresa, auth_headers):
    empresa = criar_empresa()

    resposta = _get(auth_headers(empresa.id), extrato_banco_id=str(uuid.uuid4()))

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Extrato não encontrado."}


def test_filtro_com_extrato_de_outra_empresa_da_404(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    outra_empresa = criar_empresa()
    banco_de_outra_empresa = criar_extrato_da_empresa(outra_empresa.id, "banco")

    resposta = _get(auth_headers(empresa.id), extrato_banco_id=str(banco_de_outra_empresa.id))

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Extrato não encontrado."}


def test_periodo_vem_do_extrato_do_banco_e_null_quando_o_banco_nao_tem_periodo(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(
        empresa.id, "banco", periodo_inicio=date(2026, 9, 1), periodo_fim=date(2026, 9, 30)
    )
    sistema = criar_extrato_da_empresa(
        empresa.id, "sistema", periodo_inicio=date(2026, 8, 1), periodo_fim=date(2026, 8, 31)
    )
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    item = _get(auth_headers(empresa.id)).json()["itens"][0]

    # vem do extrato do BANCO, nunca do sistema (decisão de 05/10).
    assert item["periodo_inicio"] == "2026-09-01"
    assert item["periodo_fim"] == "2026-09-30"


def test_periodo_null_quando_extrato_do_banco_nao_tem_periodo(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")  # sem período
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    assert _post_conciliacao(auth_headers, empresa.id, banco.id, sistema.id).status_code == 201

    item = _get(auth_headers(empresa.id)).json()["itens"][0]

    assert item["periodo_inicio"] is None
    assert item["periodo_fim"] is None


# CHECK ck_execucoes_conciliacao_total_e_soma_das_categorias


def test_check_total_diferente_da_soma_das_categorias_levanta_integrity_error(
    db_session, criar_empresa, criar_extrato_da_empresa
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    execucao = ExecucaoConciliacao(
        empresa_id=empresa.id,
        extrato_banco_id=banco.id,
        extrato_sistema_id=sistema.id,
        tolerancia_dias=0,
        total=5,  # deveria ser 3 (1 + 2 + 0 + 0 + 0 + 0 + 0)
        match_exato=1,
        match_tolerancia=2,
        duplicado=0,
        sem_correspondencia=0,
        tarifa_bancaria=0,
        divergente_valor=0,
        divergente_data=0,
    )
    db_session.add(execucao)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()  # limpa a sessão pra teardown do criar_empresa funcionar
