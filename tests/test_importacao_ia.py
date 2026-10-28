"""Rotas da importação via IA (issue #158, ADR-013): prompt, importação para
rascunho, descarte, expiração e o rascunho fora de toda conciliação,
listagem e contagem."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api.extratos import TAMANHO_MAXIMO_BYTES
from app.core.auth import CABECALHO_EMPRESA
from app.models import Extrato, Lancamento
from app.services.importacao_ia.leitura import ARQUIVO_CORTADO
from app.services.importacao_ia.rascunho import LIMPEZA_POR_IMPORTACAO
from main import app

client = TestClient(app)

OFX = (Path(__file__).parent / "fixtures" / "extrato_valido.ofx").read_bytes()
SECRETA = "Pix para Maria da Silva"
LANCAMENTOS = [
    {"data": "2026-09-05", "valor": "-1234.56", "descricao": "Pagamento fornecedor"},
    {"data": "2026-09-02", "valor": "500.00", "descricao": SECRETA},
    {"data": "2026-09-05", "valor": "-1234.56", "descricao": "pagamento  FORNECEDOR"},
    {"data": "2026-09-30", "valor": "10.5", "descricao": "Juros"},
]
VALIDO = json.dumps({"versao": "1", "lancamentos": LANCAMENTOS}, ensure_ascii=False).encode()
NAO_ENCONTRADA = {"detail": "Importação não encontrada."}


@pytest.fixture
def conta(db_session, criar_usuario, gerar_token, criar_empresa):
    """`conta()` cria uma conta com um admin e a empresa dele; `outra=True`
    acrescenta uma segunda empresa cliente na mesma conta."""

    def _nova(outra: bool = False):
        usuario = criar_usuario(nome="Ana Admin")
        token = gerar_token(usuario.conta_id, usuario_id=usuario.id)
        empresa_id = empresa_unica(db_session, usuario.conta_id)
        contexto = SimpleNamespace(
            usuario=usuario,
            token=token,
            empresa_id=empresa_id,
            headers=cabecalhos(token, empresa_id),
        )
        if outra:
            segunda = criar_empresa("Cliente 2", conta_id=usuario.conta_id)
            contexto.outra_id = segunda.id
            contexto.headers_outra = cabecalhos(token, segunda.id)
        return contexto

    return _nova


def _importar(headers, conteudo: bytes = VALIDO, nome: str = "relatorio.json"):
    return client.post(
        "/importacoes-ia",
        headers=headers,
        files={"arquivo": (nome, conteudo, "application/octet-stream")},
    )


def _extratos(db_session, empresa_id) -> list[Extrato]:
    db_session.expire_all()
    return db_session.scalars(select(Extrato).where(Extrato.empresa_id == empresa_id)).all()


def _lancamentos(db_session, extrato_id) -> list[Lancamento]:
    return db_session.scalars(
        select(Lancamento).where(Lancamento.extrato_id == extrato_id).order_by(Lancamento.posicao)
    ).all()


def _envelhecer(db_session, extrato_id, dias: float) -> None:
    db_session.execute(
        update(Extrato)
        .where(Extrato.id == extrato_id)
        .values(criado_em=func.now() - timedelta(days=dias))
    )
    db_session.commit()


# --- prompt ---------------------------------------------------------------------------


def test_prompt_com_as_duas_versoes_e_o_schema(db_session, conta, criar_empresa):
    c = conta(outra=True)

    # Rota da conta: não pede a empresa, mesmo com duas.
    resposta = client.get("/importacoes-ia/prompt", headers=cabecalhos(c.token))

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo) == {"versao_schema", "versao_prompt", "prompt", "schema"}
    assert (corpo["versao_schema"], corpo["versao_prompt"]) == ("1", "1")
    assert corpo["prompt"].startswith("Você vai converter")
    assert corpo["schema"]["properties"]["versao"] == {"const": "1"}


def test_prompt_exige_login():
    assert client.get("/importacoes-ia/prompt").status_code == 401


# --- importar -------------------------------------------------------------------------


def test_importacao_valida_vira_rascunho(db_session, conta):
    c = conta()

    resposta = _importar(c.headers)

    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    (extrato,) = _extratos(db_session, c.empresa_id)
    assert corpo["importacao_id"] == str(extrato.id)
    assert (corpo["status"], corpo["quantidade_lancamentos"]) == ("rascunho", 4)
    assert (corpo["periodo_inicio"], corpo["periodo_fim"]) == ("2026-09-02", "2026-09-30")
    assert (corpo["total_entradas"], corpo["total_saidas"]) == ("510.50", "-2469.12")
    expira_em = datetime.fromisoformat(corpo["expira_em"])
    assert expira_em == extrato.criado_em.replace(tzinfo=UTC) + timedelta(days=7)
    assert (extrato.origem, extrato.formato, extrato.status) == ("sistema", "json", "rascunho")
    assert (extrato.nome_arquivo, extrato.tamanho_bytes) == ("relatorio.json", len(VALIDO))
    assert (extrato.saldo_final, extrato.arquivo_chave) == (None, None)

    lancamentos = _lancamentos(db_session, extrato.id)
    assert [(item.posicao, str(item.valor), item.tipo) for item in lancamentos] == [
        (0, "-1234.56", "debito"),
        (1, "500.00", "credito"),
        (2, "-1234.56", "debito"),
        (3, "10.50", "credito"),
    ]
    # A 1ª e a 3ª são a mesma linha (descrição normalizada): ocorrências 1 e 2.
    assert [item.ocorrencia for item in lancamentos] == [1, 1, 2, 1]
    assert len({item.hash_dedup for item in lancamentos}) == 4
    assert {item.empresa_id for item in lancamentos} == {c.empresa_id}


def test_aceita_bloco_markdown_em_txt(db_session, conta):
    c = conta()
    conteudo = b"```json\n" + VALIDO + b"\n```\n"

    resposta = _importar(c.headers, conteudo, nome="resposta da IA.txt")

    assert resposta.status_code == 201
    assert resposta.json()["quantidade_lancamentos"] == 4


@pytest.mark.parametrize("nome", ["relatorio.pdf", "relatorio.csv", "relatorio", "a.json.exe"])
def test_extensao_fora_e_400(db_session, conta, nome):
    c = conta()

    resposta = _importar(c.headers, nome=nome)

    assert resposta.status_code == 400
    assert resposta.json() == {
        "detail": "Formato de arquivo não suportado. Envie o arquivo .json ou .txt com o que a "
        "IA devolveu."
    }
    assert _extratos(db_session, c.empresa_id) == []


def test_limite_de_tamanho_do_upload(db_session, conta):
    c = conta()
    grande = VALIDO + b" " * (TAMANHO_MAXIMO_BYTES - len(VALIDO) + 1)

    no_limite = _importar(c.headers, grande[:TAMANHO_MAXIMO_BYTES])
    acima = _importar(c.headers, grande)

    assert no_limite.status_code == 201
    assert acima.status_code == 413
    assert acima.json() == {"detail": "Arquivo excede o limite de 5MB."}
    assert len(_extratos(db_session, c.empresa_id)) == 1


def test_limite_de_frequencia_do_upload(db_session, conta):
    c = conta()

    respostas = [_importar(c.headers, b"{}").status_code for _ in range(11)]

    assert respostas == [422] * 10 + [429]


def test_fora_do_schema_e_422_com_todos_os_erros(db_session, conta):
    c = conta()
    documento = {
        "versao": "1",
        "lancamentos": [
            {"data": "05/09/2026", "valor": -10, "descricao": SECRETA},
            {"data": "2026-09-05", "valor": "1.234,56", "descricao": "x", "saldo": "1"},
        ],
    }

    resposta = _importar(c.headers, json.dumps(documento).encode())

    assert resposta.status_code == 422
    assert resposta.json() == {
        "detail": "O arquivo não está no formato do Ledgr. Corrija os pontos abaixo e envie de "
        "novo.",
        "erros": [
            {
                "caminho": "lancamentos[0].data",
                "lancamento": 1,
                "mensagem": 'use o formato AAAA-MM-DD, como "2026-09-05" (veio "05/09/2026")',
            },
            {
                "caminho": "lancamentos[0].valor",
                "lancamento": 1,
                "mensagem": 'use o número entre aspas, como "-1234.56" (veio -10, sem aspas)',
            },
            {
                "caminho": "lancamentos[1].saldo",
                "lancamento": 2,
                "mensagem": "campo não aceito: aqui o Ledgr só lê data, valor, descricao",
            },
            {
                "caminho": "lancamentos[1].valor",
                "lancamento": 2,
                "mensagem": "use número com ponto decimal e no máximo duas casas, sem separador "
                'de milhar nem R$, com sinal de menos na saída, como "-1234.56" (veio '
                '"1.234,56")',
            },
        ],
        "total_erros": 4,
    }
    assert _extratos(db_session, c.empresa_id) == []


def test_versao_desconhecida_e_422(db_session, conta):
    c = conta()

    resposta = _importar(c.headers, b'{"versao": "2", "lancamentos": []}')

    assert resposta.status_code == 422
    assert resposta.json()["erros"] == [
        {
            "caminho": "versao",
            "lancamento": None,
            "mensagem": 'versão "2" não é aceita: use o prompt atual do Ledgr, que gera a versão '
            '"1"',
        }
    ]


@pytest.mark.parametrize(
    "conteudo",
    [VALIDO[: len(VALIDO) // 2], VALIDO[:-2], b"```json\n" + VALIDO],
    ids=["no_meio", "sem_o_fim", "bloco_sem_fechamento"],
)
def test_cortado_e_422_so_com_detail(db_session, conta, conteudo):
    c = conta()

    resposta = _importar(c.headers, conteudo)

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": ARQUIVO_CORTADO}
    assert _extratos(db_session, c.empresa_id) == []


@pytest.mark.parametrize(
    ("conteudo", "trecho"),
    [
        (b"", "O arquivo está vazio."),
        ("Pagamento".encode("latin-1") + "é".encode("latin-1"), "UTF-8"),
        (b"Aqui esta o JSON: " + VALIDO, "sem texto antes ou depois"),
        (b'{"versao": "1",}', "não é um JSON válido"),
    ],
    ids=["vazio", "latin1", "texto_antes", "json_quebrado"],
)
def test_ilegivel_e_422_so_com_detail(db_session, conta, conteudo, trecho):
    c = conta()

    resposta = _importar(c.headers, conteudo)

    assert resposta.status_code == 422
    assert set(resposta.json()) == {"detail"}
    assert trecho in resposta.json()["detail"]


def test_log_nao_tem_descricao_nem_valor(db_session, conta, caplog):
    c = conta()
    ruim = json.dumps({"versao": "1", "lancamentos": [{**LANCAMENTOS[1], "valor": "1,5"}]})

    with caplog.at_level(logging.DEBUG):
        _importar(c.headers)
        _importar(c.headers, ruim.encode())
        _importar(c.headers, VALIDO[:50])

    texto = caplog.text
    assert SECRETA not in texto
    assert "1234.56" not in texto and "1,5" not in texto


def test_importar_exige_login_e_empresa_da_conta(db_session, conta):
    c = conta()
    outra = conta()

    sem_login = client.post(
        "/importacoes-ia", files={"arquivo": ("r.json", VALIDO, "application/json")}
    )
    empresa_de_outra_conta = _importar({**c.headers, CABECALHO_EMPRESA: str(outra.empresa_id)})

    assert sem_login.status_code == 401
    assert empresa_de_outra_conta.status_code == 404
    assert _extratos(db_session, outra.empresa_id) == []


# --- o rascunho não é extrato até ser confirmado --------------------------------------


def test_rascunho_fora_da_listagem_e_do_total(db_session, conta):
    c = conta()
    sistema = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "banco"},
        files={"arquivo": ("banco.ofx", OFX, "application/x-ofx")},
    )
    assert sistema.status_code == 201
    _importar(c.headers)

    for params in ({}, {"origem": "sistema"}):
        lista = client.get("/extratos", headers=c.headers, params=params).json()
        assert all(item["status"] != "rascunho" for item in lista["itens"])
        assert lista["total"] == (1 if params == {} else 0)
    assert client.get(
        "/extratos", headers=c.headers, params={"status": "rascunho"}
    ).status_code == (422)


def test_rascunho_e_404_nas_rotas_de_extrato_e_de_conciliacao(db_session, conta):
    c = conta()
    banco = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "banco"},
        files={"arquivo": ("banco.ofx", OFX, "application/x-ofx")},
    ).json()["extrato_id"]
    rascunho = _importar(c.headers).json()["importacao_id"]
    nao_encontrado = {"detail": "Extrato não encontrado."}

    respostas = {
        "detalhe": client.get(f"/extratos/{rascunho}", headers=c.headers),
        "conciliar": client.post(
            "/conciliacoes",
            headers=c.headers,
            json={"extrato_banco_id": banco, "extrato_sistema_id": rascunho},
        ),
        "consultar": client.get(f"/conciliacoes/{rascunho}", headers=c.headers),
        "execucoes": client.get(
            "/execucoes", headers=c.headers, params={"extrato_banco_id": rascunho}
        ),
    }

    for nome, resposta in respostas.items():
        assert resposta.status_code == 404, nome
        assert resposta.json() == nao_encontrado, nome
    assert client.get("/execucoes", headers=c.headers).json()["total"] == 0


# --- descartar ------------------------------------------------------------------------


def test_descartar_apaga_sem_residuo(db_session, conta):
    c = conta()
    importacao_id = _importar(c.headers).json()["importacao_id"]
    antes = db_session.scalar(select(func.count()).select_from(Lancamento))

    resposta = client.delete(f"/importacoes-ia/{importacao_id}", headers=c.headers)

    assert resposta.status_code == 204
    assert _extratos(db_session, c.empresa_id) == []
    assert _lancamentos(db_session, uuid.UUID(importacao_id)) == []
    assert db_session.scalar(select(func.count()).select_from(Lancamento)) == antes - 4
    de_novo = client.delete(f"/importacoes-ia/{importacao_id}", headers=c.headers)
    assert (de_novo.status_code, de_novo.json()) == (404, NAO_ENCONTRADA)


def test_descartar_nao_mexe_em_extrato_de_verdade(db_session, conta):
    c = conta()
    extrato_id = client.post(
        "/extratos/upload",
        headers=c.headers,
        data={"origem": "sistema"},
        files={"arquivo": ("banco.ofx", OFX, "application/x-ofx")},
    ).json()["extrato_id"]

    resposta = client.delete(f"/importacoes-ia/{extrato_id}", headers=c.headers)

    assert (resposta.status_code, resposta.json()) == (404, NAO_ENCONTRADA)
    assert len(_extratos(db_session, c.empresa_id)) == 1


def test_descartar_respeita_a_empresa_ativa_e_a_conta(db_session, conta):
    c = conta(outra=True)
    outra_conta = conta()
    importacao_id = _importar(c.headers).json()["importacao_id"]

    na_outra_empresa = client.delete(f"/importacoes-ia/{importacao_id}", headers=c.headers_outra)
    de_outra_conta = client.delete(f"/importacoes-ia/{importacao_id}", headers=outra_conta.headers)
    com_a_empresa_de_outra_conta = client.delete(
        f"/importacoes-ia/{importacao_id}",
        headers={**outra_conta.headers, CABECALHO_EMPRESA: str(c.empresa_id)},
    )

    assert (na_outra_empresa.status_code, na_outra_empresa.json()) == (404, NAO_ENCONTRADA)
    assert (de_outra_conta.status_code, de_outra_conta.json()) == (404, NAO_ENCONTRADA)
    assert com_a_empresa_de_outra_conta.status_code == 404
    assert com_a_empresa_de_outra_conta.json() == {"detail": "Empresa não encontrada."}
    assert len(_extratos(db_session, c.empresa_id)) == 1


def test_rascunho_e_por_empresa(db_session, conta):
    c = conta(outra=True)

    _importar(c.headers)
    _importar(c.headers_outra)

    assert len(_extratos(db_session, c.empresa_id)) == 1
    assert len(_extratos(db_session, c.outra_id)) == 1
    lista = client.get("/extratos", headers=c.headers_outra).json()
    assert lista["total"] == 0


# --- expiração ------------------------------------------------------------------------


def test_rascunho_vence_em_7_dias(db_session, conta):
    c = conta()
    quase = _importar(c.headers).json()["importacao_id"]
    vencido = _importar(c.headers).json()["importacao_id"]
    _envelhecer(db_session, quase, 6.9)
    _envelhecer(db_session, vencido, 7.1)

    assert client.delete(f"/importacoes-ia/{vencido}", headers=c.headers).json() == NAO_ENCONTRADA
    assert client.delete(f"/importacoes-ia/{quase}", headers=c.headers).status_code == 204


def test_importacao_nova_apaga_os_vencidos_de_qualquer_conta(db_session, conta):
    c = conta()
    outra = conta()
    vencido = _importar(c.headers).json()["importacao_id"]
    vigente = _importar(c.headers).json()["importacao_id"]
    _envelhecer(db_session, vencido, 8)

    assert _importar(outra.headers).status_code == 201

    restantes = {str(extrato.id) for extrato in _extratos(db_session, c.empresa_id)}
    assert restantes == {vigente}
    assert _lancamentos(db_session, uuid.UUID(vencido)) == []


def test_limpeza_apaga_no_maximo_100_os_mais_antigos_primeiro(db_session, conta):
    c = conta()
    outra = conta()
    agora = db_session.scalar(select(func.now())).replace(tzinfo=None)
    vencidos = [
        Extrato(
            id=uuid.uuid4(),
            empresa_id=c.empresa_id,
            nome_arquivo="r.json",
            formato="json",
            tamanho_bytes=1,
            origem="sistema",
            status="rascunho",
            quantidade_lancamentos=0,
            criado_em=agora - timedelta(days=30, minutes=i),
        )
        for i in range(LIMPEZA_POR_IMPORTACAO + 1)
    ]
    db_session.add_all(vencidos)
    db_session.commit()
    mais_novo = vencidos[0].id

    _importar(outra.headers)

    assert [extrato.id for extrato in _extratos(db_session, c.empresa_id)] == [mais_novo]
    _importar(outra.headers)
    assert _extratos(db_session, c.empresa_id) == []
