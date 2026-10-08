"""Testes do `GET /conciliacoes/{extrato_id}/exportar` (issue #26) contra
Postgres real. Reusa os helpers de tests/test_conciliacoes.py (fábricas de
extrato/lançamento e o `_post` que roda `POST /conciliacoes`) — mesma
validação e mesma query da listagem (`_validar_extrato_banco`/
`_construir_consulta_itens` em app/api/conciliacoes.py), então o cenário de
dados é montado do mesmo jeito que os testes do GET/POST.

A formatação (rótulos, data, valor, score, neutralização de fórmula) já é
testada isoladamente em tests/test_exportacao.py; aqui o foco é o endpoint:
validação, filtros, paginação (ausente de propósito) e os headers do CSV.
"""

import csv
import io
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_conciliacoes import _lancamento_em, _post

from app.models import Configuracao
from main import app

client = TestClient(app)


def _exportar(headers, extrato_id, **params):
    return client.get(f"/conciliacoes/{extrato_id}/exportar", headers=headers, params=params)


def _linhas_csv(conteudo: bytes) -> list[list[str]]:
    assert conteudo[:3] == b"\xef\xbb\xbf"
    texto = conteudo[3:].decode("utf-8")
    return list(csv.reader(io.StringIO(texto), delimiter=";"))


CABECALHO_ESPERADO = [
    "Status",
    "Data (banco)",
    "Valor (banco)",
    "Descrição (banco)",
    "Data (sistema)",
    "Valor (sistema)",
    "Descrição (sistema)",
    "Regra aplicada",
    "Score de confiança",
]


def _cenario_quatro_categorias(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    """Par casado + sobra só do banco + sobra só do sistema + 1 tolerância
    (tolerancia_dias_default=2), sem colisão de data/valor entre as
    categorias (ver contas na issue #26)."""
    empresa = criar_empresa()
    db_session.add(Configuracao(empresa_id=empresa.id, tolerancia_dias_default=2))
    db_session.commit()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    _lancamento_em(db_session, banco, "10.00", date(2026, 9, 5), "Pagamento fornecedor A")
    _lancamento_em(db_session, sistema, "10.00", date(2026, 9, 5), "Pagamento fornecedor A")
    _lancamento_em(db_session, banco, "50.00", date(2026, 9, 6), "Saida tolerancia banco")
    _lancamento_em(db_session, sistema, "50.00", date(2026, 9, 7), "Entrada tolerancia sistema")
    _lancamento_em(db_session, banco, "77.00", date(2026, 9, 10), "Saida so banco")
    _lancamento_em(db_session, sistema, "88.00", date(2026, 9, 11), "Entrada so sistema")

    assert _post(auth_headers(empresa.id), banco.id, sistema.id).status_code == 201
    return empresa, banco, sistema


@pytest.fixture
def cenario(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    return _cenario_quatro_categorias(
        db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
    )


def test_200_com_content_type_content_disposition_cache_control_e_bom(cenario, auth_headers):
    empresa, banco, _sistema = cenario

    response = _exportar(auth_headers(empresa.id), banco.id)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="conciliacao-{str(banco.id)[:8]}.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.content[:3] == b"\xef\xbb\xbf"


def test_cabecalho_exato(cenario, auth_headers):
    empresa, banco, _sistema = cenario

    linhas = _linhas_csv(_exportar(auth_headers(empresa.id), banco.id).content)

    assert linhas[0] == CABECALHO_ESPERADO


def test_conteudo_cobre_par_casado_tolerancia_sobra_banco_e_sobra_sistema(cenario, auth_headers):
    empresa, banco, _sistema = cenario

    linhas = _linhas_csv(_exportar(auth_headers(empresa.id), banco.id).content)
    corpo = linhas[1:]

    assert len(corpo) == 4
    match, tolerancia, so_banco, so_sistema = corpo

    assert match == [
        "Match exato", "05/09/2026", "10,00", "Pagamento fornecedor A",
        "05/09/2026", "10,00", "Pagamento fornecedor A", "Exato", "1,000",
    ]  # fmt: skip
    assert tolerancia == [
        "Match por tolerância de data", "06/09/2026", "50,00", "Saida tolerancia banco",
        "07/09/2026", "50,00", "Entrada tolerancia sistema", "Tolerância de data", "0,667",
    ]  # fmt: skip
    assert so_banco == [
        "Sem correspondência", "10/09/2026", "77,00", "Saida so banco", "", "", "", "", "",
    ]  # fmt: skip
    assert so_sistema == [
        "Sem correspondência", "", "", "", "11/09/2026", "88,00", "Entrada so sistema", "", "",
    ]  # fmt: skip


def test_filtro_por_status_devolve_so_aquelas_linhas(cenario, auth_headers):
    empresa, banco, _sistema = cenario

    linhas = _linhas_csv(
        _exportar(auth_headers(empresa.id), banco.id, status="sem_correspondencia").content
    )

    assert len(linhas) - 1 == 2
    assert {linha[0] for linha in linhas[1:]} == {"Sem correspondência"}


def test_filtro_por_extrato_sistema_id(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
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

    do_par_1 = _linhas_csv(
        _exportar(headers, banco.id, extrato_sistema_id=str(sistema_1.id)).content
    )
    do_par_2 = _linhas_csv(
        _exportar(headers, banco.id, extrato_sistema_id=str(sistema_2.id)).content
    )

    assert len(do_par_1) - 1 == 2  # par 1: 1 match + 1 duplicado (excedente 2.00 do banco)
    assert len(do_par_2) - 1 == 3  # par 2: 1 match + 1 sem_correspondencia (1.00 do banco)


def test_filtro_por_status_e_extrato_sistema_id_juntos(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema_1 = criar_extrato_da_empresa(empresa.id, "sistema")
    sistema_2 = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("1.00", "a"), ("2.00", "b")])
    inserir_lancamentos(sistema_1, [("1.00", "a")])
    inserir_lancamentos(sistema_2, [("2.00", "b")])
    headers = auth_headers(empresa.id)
    assert _post(headers, banco.id, sistema_1.id).status_code == 201
    assert _post(headers, banco.id, sistema_2.id).status_code == 201

    linhas = _linhas_csv(
        _exportar(
            headers, banco.id, status="match_exato", extrato_sistema_id=str(sistema_2.id)
        ).content
    )

    assert len(linhas) - 1 == 1
    assert linhas[1][0] == "Match exato"


def test_sem_filtro_numero_de_linhas_bate_com_o_total_da_listagem_paginada(cenario, auth_headers):
    empresa, banco, _sistema = cenario
    headers = auth_headers(empresa.id)

    listagem = client.get(f"/conciliacoes/{banco.id}", headers=headers, params={"limit": 1000})
    linhas = _linhas_csv(_exportar(headers, banco.id).content)

    assert listagem.status_code == 200
    assert len(linhas) - 1 == listagem.json()["total"]


def test_descricao_maliciosa_ganha_apostrofo_mas_valor_negativo_fica_intacto(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    _lancamento_em(db_session, banco, "-150.00", date(2026, 9, 5), '=HYPERLINK("http://evil")')
    _lancamento_em(db_session, sistema, "-88.00", date(2026, 9, 6), "-cmd")
    headers = auth_headers(empresa.id)
    assert _post(headers, banco.id, sistema.id).status_code == 201

    linhas = _linhas_csv(_exportar(headers, banco.id).content)
    corpo = linhas[1:]

    descricao_banco = next(linha[3] for linha in corpo if linha[3])
    descricao_sistema = next(linha[6] for linha in corpo if linha[6])
    valor_banco = next(linha[2] for linha in corpo if linha[2])

    assert descricao_banco == '\'=HYPERLINK("http://evil")'
    assert descricao_sistema == "'-cmd"
    assert valor_banco == "-150,00"  # valor negativo intacto, sem apóstrofo


def test_extrato_sem_conciliacao_devolve_so_o_cabecalho(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    linhas = _linhas_csv(_exportar(auth_headers(empresa.id), banco.id).content)

    assert linhas == [CABECALHO_ESPERADO]


def test_extrato_de_outra_empresa_e_inexistente_retornam_404(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    outra = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")

    de_outra = _exportar(auth_headers(outra.id), banco.id)
    inexistente = _exportar(auth_headers(dona.id), uuid.uuid4())

    assert de_outra.status_code == inexistente.status_code == 404
    assert de_outra.json()["detail"] == "Extrato não encontrado."


def test_extrato_de_origem_sistema_retorna_422(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")

    assert _exportar(auth_headers(empresa.id), sistema.id).status_code == 422


def test_status_invalido_retorna_422(criar_empresa, criar_extrato_da_empresa, auth_headers):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")

    assert _exportar(auth_headers(empresa.id), banco.id, status="inexistente").status_code == 422


def test_sem_token_retorna_401():
    assert client.get(f"/conciliacoes/{uuid.uuid4()}/exportar").status_code == 401
