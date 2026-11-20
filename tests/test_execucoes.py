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
from conftest import empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Extrato, Lancamento
from app.services.execucoes import calcular_percentual_acerto
from app.services.travas import competencia_do_extrato
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
        if periodo_inicio is not None and periodo_fim is None:
            periodo_fim = periodo_inicio  # o CHECK do banco exige os dois ou nenhum
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


# contagens.justificadas (issue #83)


def test_justificadas_conta_so_divergentes_justificadas_e_zero_nas_nao_atuais(
    db_session,
    criar_usuario,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    usuario = criar_usuario()
    headers = {"Authorization": f"Bearer {gerar_token(usuario.conta_id, usuario_id=usuario.id)}"}
    banco = criar_extrato_da_empresa(empresa_unica(db_session, usuario.conta_id), "banco")
    sistema = criar_extrato_da_empresa(empresa_unica(db_session, usuario.conta_id), "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b"), ("30.00", "c")], date(2026, 9, 5))
    inserir_lancamentos(sistema, [("10.00", "a")], date(2026, 9, 5))

    def conciliar():
        resposta = client.post(
            "/conciliacoes",
            headers=headers,
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )
        assert resposta.status_code == 201

    def decidir(valor, tipo, texto=None):
        itens = client.get(
            f"/conciliacoes/{banco.id}",
            headers=headers,
            params={"extrato_sistema_id": str(sistema.id)},
        ).json()["itens"]
        chave = next(
            item["chave"]
            for item in itens
            if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == valor
        )
        resposta = client.post(
            f"/conciliacoes/{banco.id}/decisoes",
            headers=headers,
            json={"chave": chave, "tipo": tipo, "texto": texto},
        )
        assert resposta.status_code == 200

    def itens_execucoes():
        return _get(headers, extrato_banco_id=str(banco.id)).json()["itens"]

    conciliar()
    decidir("20.00", "justificada", "motivo")
    decidir("30.00", "conferida")  # conferida não conta

    # Desde a issue #132, a justificativa nasce aguardando aprovação.
    (item,) = itens_execucoes()
    contagens = item["contagens"]
    assert (contagens["justificadas"], contagens["aguardando_aprovacao"]) == (0, 1)

    decidir("20.00", "justificativa_aprovada")
    (item,) = itens_execucoes()
    contagens = item["contagens"]
    assert (contagens["justificadas"], contagens["aguardando_aprovacao"]) == (1, 0)
    assert contagens["total"] == 3
    assert contagens["total"] == sum(
        valor
        for chave, valor in contagens.items()
        if chave not in ("total", "justificadas", "aguardando_aprovacao")
    )

    conciliar()  # a execução anterior deixa de ser atual
    atual, anterior = itens_execucoes()
    assert atual["atual"] is True and atual["contagens"]["justificadas"] == 1
    assert anterior["atual"] is False and anterior["contagens"]["justificadas"] == 0
    assert anterior["contagens"]["aguardando_aprovacao"] == 0

    # Desde a issue #164, a aprovada só sai depois de desfeita a aprovação.
    decidir("20.00", "aprovacao_desfeita")
    contagens = itens_execucoes()[0]["contagens"]
    assert (contagens["justificadas"], contagens["aguardando_aprovacao"]) == (0, 1)
    decidir("20.00", "justificativa_desfeita")
    contagens = itens_execucoes()[0]["contagens"]
    assert (contagens["justificadas"], contagens["aguardando_aprovacao"]) == (0, 0)


# filtro ?competencia= (issue #186, frontend#309)

TEXTO_COMPETENCIA_INVALIDA = "A competência deve estar no formato AAAA-MM, com mês de 01 a 12."


@pytest.fixture
def inserir_execucoes(db_session):
    """Grava execuções direto na tabela (sem rodar o matching), com `criado_em`
    crescente pra a ordem da listagem ser previsível. Devolve os ids na ordem
    de criação."""

    def _inserir(
        empresa_id, banco: Extrato, sistema: Extrato, quantidade: int = 1, desde: int = 0
    ) -> list[uuid.UUID]:
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        execucoes = [
            ExecucaoConciliacao(
                empresa_id=empresa_id,
                extrato_banco_id=banco.id,
                extrato_sistema_id=sistema.id,
                tolerancia_dias=0,
                total=1,
                match_exato=1,
                match_tolerancia=0,
                duplicado=0,
                sem_correspondencia=0,
                tarifa_bancaria=0,
                divergente_valor=0,
                divergente_data=0,
                criado_em=base + timedelta(minutes=desde + indice),
            )
            for indice in range(quantidade)
        ]
        db_session.add_all(execucoes)
        db_session.commit()
        return [execucao.id for execucao in execucoes]

    return _inserir


def _ids(resposta) -> list[str]:
    return [item["id"] for item in resposta.json()["itens"]]


def test_competencia_devolve_so_as_execucoes_do_mes(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    bancos = {
        "2026-08": criar_extrato_da_empresa(
            empresa.id, "banco", periodo_inicio=date(2026, 8, 1), periodo_fim=date(2026, 8, 31)
        ),
        "2026-09": criar_extrato_da_empresa(
            empresa.id, "banco", periodo_inicio=date(2026, 9, 1), periodo_fim=date(2026, 9, 30)
        ),
        "2026-10": criar_extrato_da_empresa(
            empresa.id, "banco", periodo_inicio=date(2026, 10, 1), periodo_fim=date(2026, 10, 31)
        ),
    }
    ids = {
        mes: inserir_execucoes(empresa.id, banco, sistema, quantidade=2, desde=indice * 10)
        for indice, (mes, banco) in enumerate(bancos.items())
    }

    for mes, esperados in ids.items():
        resposta = _get(auth_headers(empresa.id), competencia=mes)

        assert resposta.status_code == 200
        assert resposta.json()["total"] == 2
        assert sorted(_ids(resposta)) == sorted(str(id_) for id_ in esperados)
        assert {item["extrato_banco_id"] for item in resposta.json()["itens"]} == {
            str(bancos[mes].id)
        }
        assert all(item["periodo_inicio"].startswith(mes) for item in resposta.json()["itens"])


def test_competencia_sem_execucao_no_mes_devolve_lista_vazia(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_execucoes(empresa.id, banco, sistema)

    resposta = _get(auth_headers(empresa.id), competencia="2026-10")

    assert resposta.status_code == 200
    assert resposta.json() == {"total": 0, "limit": 20, "offset": 0, "itens": []}


def test_competencia_extrato_que_cruza_dois_meses_fica_no_mes_em_que_comeca(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(
        empresa.id, "banco", periodo_inicio=date(2026, 8, 28), periodo_fim=date(2026, 9, 3)
    )
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_execucoes(empresa.id, banco, sistema)

    assert _get(auth_headers(empresa.id), competencia="2026-08").json()["total"] == 1
    assert _get(auth_headers(empresa.id), competencia="2026-09").json()["total"] == 0


def test_competencia_coincide_com_competencia_do_extrato_nas_bordas_do_mes(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inicios = [
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2026, 12, 31),
        date(2027, 1, 1),
        date(2028, 2, 29),
        date(2028, 3, 1),
    ]
    extratos = [
        criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=inicio) for inicio in inicios
    ]
    id_da_execucao = {
        extrato.id: inserir_execucoes(empresa.id, extrato, sistema)[0] for extrato in extratos
    }

    for competencia in [
        "2026-08",
        "2026-09",
        "2026-10",
        "2026-12",
        "2027-01",
        "2028-02",
        "2028-03",
    ]:
        esperados = {
            str(id_da_execucao[extrato.id])
            for extrato in extratos
            if competencia_do_extrato(extrato) == competencia
        }
        resposta = _get(auth_headers(empresa.id), competencia=competencia)

        assert esperados  # cada mês testado tem ao menos um extrato
        assert set(_ids(resposta)) == esperados, competencia
        assert resposta.json()["total"] == len(esperados), competencia


def test_competencia_total_respeita_o_filtro_e_o_offset_percorre_tudo(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    banco_set = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    banco_out = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 10, 1))
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    esperados = inserir_execucoes(empresa.id, banco_set, sistema, quantidade=13)
    inserir_execucoes(empresa.id, banco_out, sistema, quantidade=4, desde=100)

    vistos: list[str] = []
    offset = 0
    while True:
        resposta = _get(auth_headers(empresa.id), competencia="2026-09", limit=5, offset=offset)
        corpo = resposta.json()
        assert corpo["total"] == 13  # o total não depende da página
        assert corpo["limit"] == 5 and corpo["offset"] == offset
        if not corpo["itens"]:
            break
        vistos.extend(_ids(resposta))
        offset += 5

    assert len(vistos) == len(set(vistos)) == 13
    assert set(vistos) == {str(id_) for id_ in esperados}
    # mais recente primeiro, igual à rota sem filtro
    assert vistos == [str(id_) for id_ in reversed(esperados)]


def test_competencia_com_mais_execucoes_que_o_limite_maximo(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    """O caso do front: mais de 50 execuções desde o começo do mês."""
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_execucoes(empresa.id, banco, sistema, quantidade=120)

    primeira = _get(auth_headers(empresa.id), competencia="2026-09", limit=100).json()
    segunda = _get(auth_headers(empresa.id), competencia="2026-09", limit=100, offset=100).json()

    assert primeira["total"] == segunda["total"] == 120
    assert len(primeira["itens"]) == 100
    assert len(segunda["itens"]) == 20


def test_competencia_combina_com_extrato_banco_id(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    banco_a = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    banco_b = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 10))
    banco_c = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 10, 1))
    ids_a = inserir_execucoes(empresa.id, banco_a, sistema, quantidade=2)
    inserir_execucoes(empresa.id, banco_b, sistema, quantidade=3, desde=10)
    inserir_execucoes(empresa.id, banco_c, sistema, quantidade=1, desde=20)
    headers = auth_headers(empresa.id)

    so_competencia = _get(headers, competencia="2026-09")
    as_duas = _get(headers, competencia="2026-09", extrato_banco_id=str(banco_a.id))
    mes_diferente = _get(headers, competencia="2026-10", extrato_banco_id=str(banco_a.id))

    assert so_competencia.json()["total"] == 5
    assert as_duas.json()["total"] == 2
    assert sorted(_ids(as_duas)) == sorted(str(id_) for id_ in ids_a)
    assert mes_diferente.status_code == 200
    assert mes_diferente.json()["total"] == 0 and mes_diferente.json()["itens"] == []


def test_competencia_extrato_banco_id_inexistente_continua_404(criar_empresa, auth_headers):
    empresa = criar_empresa()

    resposta = _get(
        auth_headers(empresa.id), competencia="2026-09", extrato_banco_id=str(uuid.uuid4())
    )

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Extrato não encontrado."}


def test_extrato_do_banco_sem_periodo_inicio_some_com_filtro_e_aparece_sem(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    com_periodo = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    sem_periodo = criar_extrato_da_empresa(empresa.id, "banco")
    id_com = inserir_execucoes(empresa.id, com_periodo, sistema)[0]
    id_sem = inserir_execucoes(empresa.id, sem_periodo, sistema, desde=10)[0]
    headers = auth_headers(empresa.id)

    sem_filtro = _get(headers)
    com_filtro = _get(headers, competencia="2026-09")

    assert sem_filtro.json()["total"] == 2
    assert set(_ids(sem_filtro)) == {str(id_com), str(id_sem)}
    assert com_filtro.json()["total"] == 1
    assert _ids(com_filtro) == [str(id_com)]
    # nenhuma competência pega o extrato sem período
    for competencia in ("2026-08", "2026-09", "2026-10", "1970-01"):
        assert str(id_sem) not in _ids(_get(headers, competencia=competencia))


def test_atual_e_igual_com_e_sem_filtro(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema_1 = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="s1.csv")
    sistema_2 = criar_extrato_da_empresa(empresa.id, "sistema", nome_arquivo="s2.csv")
    banco_set = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    banco_out = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 10, 1))
    # várias rodadas do mesmo extrato do banco: com o mesmo par e com outro sistema
    inserir_execucoes(empresa.id, banco_set, sistema_1, quantidade=3)
    inserir_execucoes(empresa.id, banco_set, sistema_2, quantidade=2, desde=10)
    inserir_execucoes(empresa.id, banco_out, sistema_1, quantidade=2, desde=20)
    headers = auth_headers(empresa.id)

    sem_filtro = {item["id"]: item["atual"] for item in _get(headers, limit=100).json()["itens"]}
    com_filtro = _get(headers, competencia="2026-09", limit=100).json()["itens"]

    assert len(com_filtro) == 5
    assert [item["atual"] for item in com_filtro].count(True) == 2  # uma por par
    for item in com_filtro:
        assert item["atual"] == sem_filtro[item["id"]]


def test_atual_continua_certo_quando_a_pagina_nao_traz_a_execucao_mais_recente(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    banco = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=date(2026, 9, 1))
    ids = inserir_execucoes(empresa.id, banco, sistema, quantidade=3)

    # offset=1 pula a mais recente: as duas antigas não podem virar "atual".
    resposta = _get(auth_headers(empresa.id), competencia="2026-09", limit=2, offset=1)

    assert _ids(resposta) == [str(ids[1]), str(ids[0])]
    assert [item["atual"] for item in resposta.json()["itens"]] == [False, False]


@pytest.mark.parametrize("competencia", ["2026-13", "2026-1", "abc", "2026-00", "", "2026-09-01"])
def test_competencia_com_formato_invalido_da_422_com_o_texto_de_fechamentos(
    competencia, criar_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)

    resposta = _get(headers, competencia=competencia)
    em_fechamentos = client.get(
        "/fechamentos", headers=headers, params={"competencia": competencia}
    )

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": TEXTO_COMPETENCIA_INVALIDA}
    assert em_fechamentos.status_code == 422
    assert em_fechamentos.json() == resposta.json()


@pytest.mark.parametrize("competencia", ["0000-01", "0000-12", "9999-12"])
def test_competencia_fora_do_calendario_do_python_da_422_e_nao_500(
    competencia, criar_empresa, auth_headers
):
    """O formato aceita 0000-01 e 9999-12, mas o intervalo do mês precisa de
    date(0, 1, 1) e date(10000, 1, 1): nada disso pode virar 500."""
    empresa = criar_empresa()

    resposta = _get(auth_headers(empresa.id), competencia=competencia)

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": TEXTO_COMPETENCIA_INVALIDA}


@pytest.mark.parametrize("competencia", ["0001-01", "9999-01", "9999-11"])
def test_competencia_nos_extremos_representaveis_continua_valida(
    competencia, criar_empresa, auth_headers
):
    empresa = criar_empresa()

    resposta = _get(auth_headers(empresa.id), competencia=competencia)

    assert resposta.status_code == 200
    assert resposta.json()["total"] == 0


@pytest.mark.parametrize("competencia", ["0000-01", "9999-12"])
def test_fechamentos_segue_aceitando_os_valores_que_execucoes_recusa(
    competencia, criar_empresa, auth_headers
):
    empresa = criar_empresa()

    resposta = client.get(
        "/fechamentos", headers=auth_headers(empresa.id), params={"competencia": competencia}
    )

    assert resposta.status_code == 200


def test_competencia_isolamento_por_empresa_ativa(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa_a = criar_empresa()
    empresa_b = criar_empresa()
    banco_a = criar_extrato_da_empresa(empresa_a.id, "banco", periodo_inicio=date(2026, 9, 1))
    sistema_a = criar_extrato_da_empresa(empresa_a.id, "sistema")
    banco_b = criar_extrato_da_empresa(empresa_b.id, "banco", periodo_inicio=date(2026, 9, 1))
    sistema_b = criar_extrato_da_empresa(empresa_b.id, "sistema")
    ids_a = inserir_execucoes(empresa_a.id, banco_a, sistema_a, quantidade=2)
    ids_b = inserir_execucoes(empresa_b.id, banco_b, sistema_b, quantidade=3)

    resposta_a = _get(auth_headers(empresa_a.id), competencia="2026-09")
    resposta_b = _get(auth_headers(empresa_b.id), competencia="2026-09")

    assert resposta_a.json()["total"] == 2
    assert set(_ids(resposta_a)) == {str(id_) for id_ in ids_a}
    assert resposta_b.json()["total"] == 3
    assert set(_ids(resposta_b)) == {str(id_) for id_ in ids_b}


def test_sem_competencia_a_rota_responde_como_antes(
    criar_empresa, criar_extrato_da_empresa, inserir_execucoes, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    for indice, inicio in enumerate([date(2026, 8, 1), date(2026, 9, 1), None]):
        banco = criar_extrato_da_empresa(empresa.id, "banco", periodo_inicio=inicio)
        inserir_execucoes(empresa.id, banco, sistema, desde=indice * 10)

    corpo = _get(auth_headers(empresa.id)).json()

    assert corpo["total"] == 3 and len(corpo["itens"]) == 3
    assert list(corpo) == ["total", "limit", "offset", "itens"]
    horarios = [item["executada_em"] for item in corpo["itens"]]
    assert horarios == sorted(horarios, reverse=True)
