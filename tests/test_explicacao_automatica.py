"""Explicação automática ao conciliar (issue #140, adendo do ADR-011).

Com `EXPLICACAO_AUTOMATICA_HABILITADA` e `LLM_HABILITADO` ligadas, o
`POST /conciliacoes` agenda, depois da resposta, a explicação das divergências
do par, e o `GET /conciliacoes/{id}` entrega `explicacao` e `gerada_por_ia`.
Nenhum teste chama a OpenAI: o provedor é um falso que conta as chamadas.
O `TestClient` roda as `BackgroundTasks` antes de devolver a resposta, então
a geração já terminou quando o teste confere (o teste que prova que a
resposta sai antes chama o app pelo ASGI direto).
"""

import asyncio
import hashlib
import json
import logging
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.core.config import Settings, settings
from app.models import Conciliacao, ExplicacaoDivergencia
from app.services.ia.base import ProvedorIAIndisponivel, RespostaIA
from app.services.ia.geracao import STATUS_COM_EXPLICACAO, obter_provedor_dependencia
from main import app

client = TestClient(app)

MATCH = ("match_exato", "match_tolerancia")


class ProvedorContador:
    """Provedor falso, seguro entre threads: responde um texto por valor do
    lançamento e conta as chamadas. `falhar_em` são valores que levantam."""

    def __init__(self, falhar_em=(), eventos=None) -> None:
        self._trava = threading.Lock()
        self.chamadas: list[tuple[Decimal, str | None]] = []
        self.falhar_em = {Decimal(v) for v in falhar_em}
        self.eventos = eventos

    def explicar_divergencia(self, contexto, *, identificador_anonimo=None):
        valor = contexto.lancamento.valor
        with self._trava:
            self.chamadas.append((valor, identificador_anonimo))
            if self.eventos is not None:
                self.eventos.append("provedor")
        if valor in self.falhar_em:
            raise ProvedorIAIndisponivel("http", status_http=500)
        return RespostaIA(
            texto=f"Explicação do lançamento de {valor}.",
            tokens_entrada=10,
            tokens_saida=5,
            modelo="falso",
        )


@pytest.fixture
def ia(monkeypatch):
    """As duas flags ligadas e o provedor falso nas rotas. Limites, teto e
    concorrência fixos: o `.env` de quem desenvolve pode ter outros valores
    (o `LLM_LIMITE_DIARIO_EMPRESA` antigo vale como limite por conta)."""
    monkeypatch.setattr(settings, "llm_habilitado", True)
    monkeypatch.setattr(settings, "explicacao_automatica_habilitada", True)
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 10_000)
    monkeypatch.setattr(settings, "llm_limite_diario_global", 100_000)
    monkeypatch.setattr(settings, "llm_teto_por_conciliacao", 100)
    monkeypatch.setattr(settings, "llm_concorrencia_automatica", 4)
    provedor = ProvedorContador()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: provedor
    yield provedor
    app.dependency_overrides.pop(obter_provedor_dependencia, None)


@pytest.fixture
def conta(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """`conta()` cria uma conta com uma empresa; `conta(conta_id=...)` acrescenta
    um usuário de uma conta existente, numa empresa nova dela."""

    def _nova(empresa_id=None):
        if empresa_id is None:
            usuario = criar_usuario()
            empresa_id = empresa_unica(db_session, usuario.conta_id)
        else:
            usuario = criar_usuario(empresa_id=empresa_id)
        headers = cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id), empresa_id)

        def extrato(origem, itens):
            novo = criar_extrato_da_empresa(empresa_id, origem)
            inserir_lancamentos(novo, list(itens))
            return novo

        def conciliar(banco, sistema):
            resposta = client.post(
                "/conciliacoes",
                headers=headers,
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == 201, resposta.text
            return resposta

        def itens(banco, sistema=None):
            params = {"limit": 1000}
            if sistema is not None:
                params["extrato_sistema_id"] = str(sistema.id)
            resposta = client.get(f"/conciliacoes/{banco.id}", headers=headers, params=params)
            assert resposta.status_code == 200
            return resposta.json()["itens"]

        return SimpleNamespace(
            usuario=usuario,
            empresa_id=empresa_id,
            headers=headers,
            extrato=extrato,
            conciliar=conciliar,
            itens=itens,
        )

    return _nova


def _valor(item) -> Decimal:
    lancamento = item["lancamento_banco"] or item["lancamento_sistema"]
    return Decimal(lancamento["valor"])


def _divergentes(itens):
    return [item for item in itens if item["status"] in STATUS_COM_EXPLICACAO]


# --- geração ao conciliar ----------------------------------------------------------


def test_divergencias_ganham_explicacao_sem_clique_e_matches_vem_null(db_session, conta, ia):
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b"), ("-5.50", "TARIFA BANCARIA")])
    sistema = c.extrato("sistema", [("10.00", "a"), ("99.00", "so sistema")])

    c.conciliar(banco, sistema)

    itens = c.itens(banco)
    divergentes = _divergentes(itens)
    assert len(divergentes) == 3
    for item in divergentes:
        assert item["explicacao"] == f"Explicação do lançamento de {_valor(item)}."
        assert item["gerada_por_ia"] is True
    (match,) = [item for item in itens if item["status"] in MATCH]
    assert (match["explicacao"], match["gerada_por_ia"]) == (None, False)
    assert len(ia.chamadas) == 3
    # O mesmo identificador anônimo do clique: o sha256 do id da empresa.
    anonimo = hashlib.sha256(str(c.empresa_id).encode()).hexdigest()
    assert {identificador for _, identificador in ia.chamadas} == {anonimo}


def test_teto_por_conciliacao_gera_as_de_maior_valor(db_session, conta, ia, monkeypatch):
    monkeypatch.setattr(settings, "llm_teto_por_conciliacao", 100)
    c = conta()
    banco = c.extrato("banco", [(f"{n}.00", f"item {n}") for n in range(1, 121)])
    sistema = c.extrato("sistema", [("999.00", "outro")])

    c.conciliar(banco, sistema)

    divergentes = sorted(_divergentes(c.itens(banco)), key=lambda i: -abs(_valor(i)))
    assert len(divergentes) == 121
    com_texto = [item for item in divergentes if item["explicacao"] is not None]
    assert com_texto == divergentes[:100]
    assert all(item["gerada_por_ia"] is False for item in divergentes[100:])
    assert len(ia.chamadas) == 100


def test_falha_numa_linha_nao_impede_as_outras(db_session, conta, ia, caplog):
    ia.falhar_em = {Decimal("20.00")}
    c = conta()
    banco = c.extrato("banco", [("20.00", "b"), ("30.00", "c"), ("40.00", "d")])
    sistema = c.extrato("sistema", [("99.00", "so sistema")])

    with caplog.at_level(logging.INFO, logger="app.services.ia.geracao"):
        c.conciliar(banco, sistema)

    por_valor = {_valor(item): item for item in _divergentes(c.itens(banco))}
    assert por_valor[Decimal("20.00")]["explicacao"] is None
    assert por_valor[Decimal("30.00")]["gerada_por_ia"] is True
    assert por_valor[Decimal("99.00")]["gerada_por_ia"] is True
    resumo = [r.getMessage() for r in caplog.records if "geradas=" in r.getMessage()]
    assert resumo == ["explicacao_automatica geradas=3 cache=0 teto=0 limite=0 falhas=1"]


def test_reconciliar_ou_rodada_igual_nao_paga_de_novo(db_session, conta, ia):
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, sistema)
    pagas = len(ia.chamadas)
    assert pagas == 2

    c.conciliar(banco, sistema)  # o mesmo par: linhas recriadas, cache por contexto
    copia = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, copia)  # rodada nova com o mesmo conteúdo

    assert len(ia.chamadas) == pagas
    divergentes = _divergentes(c.itens(banco, copia))
    assert divergentes and all(item["gerada_por_ia"] for item in divergentes)


def test_rodada_nova_com_mudanca_paga_so_o_que_mudou(db_session, conta, ia):
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, sistema)
    antes = len(ia.chamadas)

    v2 = c.extrato("sistema", [("10.00", "a"), ("77.00", "novo")])
    c.conciliar(banco, v2)

    divergentes = _divergentes(c.itens(banco, v2))
    novas = len(ia.chamadas) - antes
    assert 1 <= novas <= len(divergentes)
    assert all(item["gerada_por_ia"] for item in divergentes)


def test_linha_reconciliada_no_meio_so_perde_a_ligacao(db_session, conta, ia):
    """O texto fica no cache; a linha nova é ligada pela rodada que a criou."""
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, sistema)
    db_session.execute(
        update(Conciliacao)
        .where(Conciliacao.extrato_banco_id == banco.id)
        .values(explicacao_chave=None)
    )
    db_session.commit()

    assert all(item["explicacao"] is None for item in c.itens(banco))
    c.conciliar(banco, sistema)
    assert all(item["gerada_por_ia"] for item in _divergentes(c.itens(banco)))


# --- limites ------------------------------------------------------------------------


def test_limite_diario_e_por_conta(db_session, conta, ia, criar_empresa, monkeypatch):
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 3)
    monkeypatch.setattr(settings, "llm_concorrencia_automatica", 1)  # sem a folga da concorrência
    a1 = conta()
    a2 = conta(empresa_id=criar_empresa("Cliente 2", conta_id=a1.usuario.conta_id).id)
    banco_1 = a1.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    a1.conciliar(banco_1, a1.extrato("sistema", [("10.00", "a")]))
    banco_2 = a2.extrato("banco", [("30.00", "c"), ("40.00", "d"), ("50.00", "e")])

    a2.conciliar(banco_2, a2.extrato("sistema", [("99.00", "x")]))

    assert sum(item["gerada_por_ia"] for item in a1.itens(banco_1)) == 1
    assert sum(item["gerada_por_ia"] for item in a2.itens(banco_2)) == 2
    assert len(ia.chamadas) == 3


def test_limite_global(db_session, conta, ia, monkeypatch):
    monkeypatch.setattr(settings, "llm_concorrencia_automatica", 1)
    ja_geradas = db_session.scalar(select(func.count()).select_from(ExplicacaoDivergencia))
    monkeypatch.setattr(settings, "llm_limite_diario_global", ja_geradas + 2)
    c = conta()
    banco = c.extrato("banco", [("20.00", "b"), ("30.00", "c"), ("40.00", "d")])

    c.conciliar(banco, c.extrato("sistema", [("99.00", "x")]))

    assert sum(item["gerada_por_ia"] for item in c.itens(banco)) == 2


@pytest.mark.parametrize(
    ("variaveis", "esperado"),
    [
        ({}, 300),
        ({"LLM_LIMITE_DIARIO_EMPRESA": "7"}, 7),
        ({"LLM_LIMITE_DIARIO_CONTA": "9"}, 9),
        ({"LLM_LIMITE_DIARIO_EMPRESA": "7", "LLM_LIMITE_DIARIO_CONTA": "9"}, 9),
    ],
    ids=["padrao", "nome_antigo", "nome_novo", "os_dois_vale_o_novo"],
)
def test_limite_por_conta_aceita_o_nome_antigo(monkeypatch, variaveis, esperado):
    for nome in ("LLM_LIMITE_DIARIO_EMPRESA", "LLM_LIMITE_DIARIO_CONTA"):
        monkeypatch.delenv(nome, raising=False)
    for nome, valor in variaveis.items():
        monkeypatch.setenv(nome, valor)

    configuracao = Settings(_env_file=None, database_url="postgresql://x/y")

    assert configuracao.llm_limite_diario_conta == esperado


@pytest.mark.parametrize("variavel", ["LLM_TETO_POR_CONCILIACAO", "LLM_CONCORRENCIA_AUTOMATICA"])
def test_teto_e_concorrencia_precisam_ser_positivos(monkeypatch, variavel):
    monkeypatch.setenv(variavel, "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x/y")


def test_padroes_da_issue_140():
    campos = Settings.model_fields
    assert campos["explicacao_automatica_habilitada"].default is False
    assert campos["llm_teto_por_conciliacao"].default == 100
    assert campos["llm_concorrencia_automatica"].default == 4
    assert campos["llm_limite_diario_global"].default == 1500


# --- flags --------------------------------------------------------------------------


def test_flag_automatica_desligada_nao_gera_mas_o_clique_aparece_no_get(
    db_session, conta, ia, monkeypatch
):
    monkeypatch.setattr(settings, "explicacao_automatica_habilitada", False)
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    c.conciliar(banco, c.extrato("sistema", [("10.00", "a")]))

    assert ia.chamadas == []
    (divergente,) = _divergentes(c.itens(banco))
    assert (divergente["explicacao"], divergente["gerada_por_ia"]) == (None, False)

    clique = client.post(
        "/explicacoes", headers=c.headers, json={"conciliacao_id": divergente["id"]}
    )

    assert clique.json()["gerada_por_ia"] is True
    (divergente,) = _divergentes(c.itens(banco))
    assert divergente["explicacao"] == clique.json()["explicacao"]
    assert divergente["gerada_por_ia"] is True


def test_clique_com_cache_tambem_liga_a_linha(db_session, conta, ia, monkeypatch):
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, sistema)
    db_session.execute(
        update(Conciliacao)
        .values(explicacao_chave=None)
        .where(Conciliacao.extrato_banco_id == banco.id)
    )
    db_session.commit()
    (divergente,) = _divergentes(c.itens(banco))

    clique = client.post(
        "/explicacoes", headers=c.headers, json={"conciliacao_id": divergente["id"]}
    )

    assert clique.json()["em_cache"] is True
    assert _divergentes(c.itens(banco))[0]["explicacao"] == clique.json()["explicacao"]


def test_llm_desligado_nao_gera_nem_mostra_o_cache(db_session, conta, ia, monkeypatch):
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    c.conciliar(banco, sistema)
    chamadas = len(ia.chamadas)

    monkeypatch.setattr(settings, "llm_habilitado", False)
    c.conciliar(banco, sistema)

    assert len(ia.chamadas) == chamadas
    assert all(
        (item["explicacao"], item["gerada_por_ia"]) == (None, False) for item in c.itens(banco)
    )


def test_sem_provedor_nada_e_agendado(db_session, conta, ia):
    app.dependency_overrides[obter_provedor_dependencia] = lambda: None
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b")])

    c.conciliar(banco, c.extrato("sistema", [("10.00", "a")]))

    assert all(item["explicacao"] is None for item in c.itens(banco))


# --- a resposta sai antes da IA ------------------------------------------------------


def test_post_conciliacoes_responde_antes_da_geracao(db_session, conta, ia):
    """Chama o app pelo ASGI: a resposta inteira sai antes da primeira chamada
    ao provedor, que roda na BackgroundTask."""
    eventos: list[str] = []
    ia.eventos = eventos
    c = conta()
    banco = c.extrato("banco", [("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    sistema = c.extrato("sistema", [("10.00", "a")])
    corpo = json.dumps(
        {"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)}
    ).encode()
    cabecalhos_asgi = [(k.lower().encode(), v.encode()) for k, v in c.headers.items()] + [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(corpo)).encode()),
    ]
    status: list[int] = []

    async def _chamar():
        entregue = False

        async def receive():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(mensagem):
            if mensagem["type"] == "http.response.start":
                status.append(mensagem["status"])
            elif mensagem["type"] == "http.response.body" and not mensagem.get("more_body"):
                eventos.append("resposta")

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/conciliacoes",
                "raw_path": b"/conciliacoes",
                "query_string": b"",
                "root_path": "",
                "headers": cabecalhos_asgi,
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

    asyncio.run(_chamar())

    assert status == [201]
    assert eventos[0] == "resposta"
    assert eventos.count("provedor") == 2


# --- acesso cruzado -----------------------------------------------------------------


def test_explicacao_de_uma_empresa_nunca_aparece_em_outra(db_session, conta, ia):
    a, b = conta(), conta()
    itens = [("10.00", "a"), ("20.00", "b")]
    banco_a = a.extrato("banco", itens)
    a.conciliar(banco_a, a.extrato("sistema", [("10.00", "a")]))
    banco_b = b.extrato("banco", itens)
    b.conciliar(banco_b, b.extrato("sistema", [("10.00", "a")]))

    # O mesmo contexto em duas empresas é pago duas vezes: não há cache comum.
    assert len(ia.chamadas) == 2
    chave_a = db_session.scalar(
        select(Conciliacao.explicacao_chave).where(
            Conciliacao.extrato_banco_id == banco_a.id,
            Conciliacao.explicacao_chave.is_not(None),
        )
    )
    # A linha da empresa B apontando para a chave da A não mostra o texto da A.
    db_session.execute(
        update(Conciliacao)
        .where(Conciliacao.extrato_banco_id == banco_b.id)
        .values(explicacao_chave=chave_a)
    )
    db_session.execute(
        ExplicacaoDivergencia.__table__.delete().where(
            ExplicacaoDivergencia.empresa_id == b.empresa_id
        )
    )
    db_session.commit()

    assert all(item["explicacao"] is None for item in b.itens(banco_b))
    assert any(item["gerada_por_ia"] for item in a.itens(banco_a))
    nao_achou = client.get(f"/conciliacoes/{banco_a.id}", headers=b.headers)
    assert nao_achou.status_code == 404
