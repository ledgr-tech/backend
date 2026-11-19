"""`POST /assistente/perguntas` com dados da empresa (issue #223, ADR-028).

Com `ASSISTENTE_DADOS_HABILITADO` e uma empresa ativa, o modelo só interpreta
a pergunta, e o backend responde as consultas com o banco e textos prontos.
Duas regras do Vitor (17/11/2026), testadas aqui:

1. nenhum dado sensível vai à IA: a pergunta e o histórico saem com
   marcadores, e o resultado das consultas nunca volta ao provedor;
2. o assistente nunca grava dados: pedido de gravar recebe recusa fixa, e a
   consulta roda numa transação só de leitura.
"""

import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.models import Fechamento, PerguntaSemResposta, UsoAssistente
from app.services.ajuda import consultas
from app.services.ajuda.consultas import mes_atual_de_sao_paulo
from app.services.ajuda.conteudo import FonteDaAjuda, obter_fonte_da_ajuda
from app.services.ia.base import ConsultaDeDados, RespostaAjuda
from app.services.ia.geracao import obter_provedor_dependencia
from main import app

client = TestClient(app)

CONTEUDO = (Path(__file__).parent / "fixtures" / "ajuda_conteudo.json").read_bytes()
URL = "https://ajuda.teste/ajuda/conteudo.json"
SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
DESCRICAO_SECRETA = "PIX RECEBIDO MARIA DA SILVA"
ARQUIVO_SECRETO = "extrato_itau_conta_98765.ofx"
SALDO_SECRETO = (Decimal("98765.43"), date(2026, 9, 30))


class ProvedorFalso:
    """Guarda o que recebeu e devolve o que o teste combinou."""

    def __init__(self) -> None:
        self.recebidas = []
        self.saida = RespostaAjuda("Use a tela Extratos.", True, (), 100, 20, "falso")

    def responder_ajuda(self, pergunta, *, identificador_anonimo=None):
        self.recebidas.append(pergunta)
        return self.saida

    def dados(self, intencao, competencia=None, meses_atras=None, categoria=None):
        self.saida = RespostaAjuda(
            "",
            True,
            (),
            100,
            20,
            "falso",
            tipo="dados",
            consulta=ConsultaDeDados(intencao, competencia, meses_atras, categoria),
        )

    def outro(self, tipo, tela=None):
        self.saida = RespostaAjuda("", False, (), 100, 20, "falso", tipo=tipo, tela=tela)


@pytest.fixture
def assistente(monkeypatch):
    monkeypatch.setattr(settings, "llm_habilitado", True)
    monkeypatch.setattr(settings, "assistente_habilitado", True)
    monkeypatch.setattr(settings, "assistente_dados_habilitado", True)
    monkeypatch.setattr(settings, "ajuda_conteudo_url", URL)
    monkeypatch.setattr(settings, "assistente_limite_diario_conta", 100)
    monkeypatch.setattr(settings, "assistente_limite_diario_global", 100_000)
    monkeypatch.setattr(settings, "assistente_maximo_conteudo_caracteres", 60000)
    provedor = ProvedorFalso()
    fonte = FonteDaAjuda(
        url=lambda: settings.ajuda_conteudo_url,
        cliente_http=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=CONTEUDO))
        ),
    )
    app.dependency_overrides[obter_provedor_dependencia] = lambda: provedor
    app.dependency_overrides[obter_fonte_da_ajuda] = lambda: fonte
    yield provedor
    app.dependency_overrides.pop(obter_provedor_dependencia, None)
    app.dependency_overrides.pop(obter_fonte_da_ajuda, None)


@pytest.fixture
def empresa(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """Admin, a empresa dele e setembro/2026 conciliado, com saldo no arquivo."""
    admin = criar_usuario()
    empresa_id = empresa_unica(db_session, admin.conta_id)
    token = gerar_token(admin.conta_id, usuario_id=admin.id)

    def extrato(origem, itens, saldo=None):
        novo = criar_extrato_da_empresa(empresa_id, origem)
        inserir_lancamentos(novo, itens)
        novo.periodo_inicio, novo.periodo_fim = SETEMBRO
        novo.nome_arquivo = ARQUIVO_SECRETO
        if saldo is not None:
            novo.saldo_final, novo.saldo_data = saldo
        db_session.commit()
        return novo

    banco = extrato("banco", [("10.00", "a"), ("3200.00", DESCRICAO_SECRETA)], SALDO_SECRETO)
    sistema = extrato("sistema", [("10.00", "a")])
    resposta = client.post(
        "/conciliacoes",
        headers=cabecalhos(token, empresa_id),
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert resposta.status_code == 201, resposta.text
    admin.empresa_id = empresa_id
    admin.banco = banco
    admin.com_empresa = cabecalhos(token, empresa_id)
    admin.sem_empresa = cabecalhos(token)
    return admin


@pytest.fixture
def apagar_perguntas(db_session):
    antes = datetime.now(UTC)
    yield
    db_session.rollback()
    db_session.execute(delete(PerguntaSemResposta).where(PerguntaSemResposta.criado_em >= antes))
    db_session.commit()


def _perguntar(headers, pergunta="como ficou setembro?", **extra):
    return client.post(
        "/assistente/perguntas", headers=headers, json={"pergunta": pergunta, **extra}
    )


def _tudo_que_foi_ao_provedor(provedor) -> str:
    partes = []
    for pergunta in provedor.recebidas:
        partes += [pergunta.pergunta, pergunta.tela or "", pergunta.mes_atual or ""]
        partes += [turno.texto for turno in pergunta.historico]
    return "\n".join(partes)


# --- regra 1: nada sensível vai à IA ------------------------------------------------------


def test_consulta_responde_com_o_banco_e_o_resultado_nao_vai_ao_provedor(assistente, empresa):
    assistente.dados("saldo", competencia="2026-09")

    resposta = _perguntar(empresa.com_empresa, "qual o saldo final de setembro?")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert "R$ 98.765,43" in corpo["resposta"] and corpo["respondida"] is True
    assert corpo["links"] == [
        {
            "titulo": "Conciliação de setembro/2026",
            "caminho": f"/conciliacoes/{empresa.banco.id}",
            "tipo": "tela",
        }
    ]
    (pergunta,) = assistente.recebidas
    assert pergunta.dados is True
    assert re.fullmatch(r"\d{4}-\d{2}", pergunta.mes_atual)
    enviado = _tudo_que_foi_ao_provedor(assistente)
    for secreto in ("98765", "98.765", DESCRICAO_SECRETA, ARQUIVO_SECRETO, str(empresa.empresa_id)):
        assert secreto not in enviado


def test_valores_datas_e_respostas_anteriores_vao_com_marcadores(assistente, empresa):
    assistente.dados("resumo_do_mes")

    _perguntar(
        empresa.com_empresa,
        "e o pix de R$ 3.200,00 do dia 12/09 do CPF 123.456.789-09?",
        historico=[
            {"papel": "usuario", "texto": "qual o saldo final de setembro?"},
            {
                "papel": "assistente",
                "texto": "Extrato do banco de setembro/2026: saldo final de R$ 98.765,43.",
            },
        ],
    )

    (pergunta,) = assistente.recebidas
    assert pergunta.pergunta == "e o pix de [VALOR_1] do dia [DATA_1] do CPF [CPF]?"
    assert pergunta.historico[1].texto == (
        "Extrato do banco de setembro/[N]: saldo final de R$ [N]."
    )
    assert not re.search(r"\d", pergunta.historico[1].texto)


def test_nomes_das_empresas_da_conta_nao_vao_ao_provedor(
    assistente, empresa, db_session, criar_empresa
):
    """A resposta de `empresas_do_mes` traz as razões sociais e volta no
    histórico; a pessoa também pode digitá-las. Nenhuma sai do backend."""
    criar_empresa(razao_social="Beta Serviços Ltda", conta_id=empresa.conta_id)
    assistente.dados("empresas_do_mes", competencia="2026-09")

    resposta = _perguntar(
        empresa.com_empresa,
        "e a beta serviços ltda?",
        historico=[
            {"papel": "assistente", "texto": "Setembro/2026: 1 aberta (Beta Serviços Ltda)."}
        ],
    )

    assert resposta.status_code == 200, resposta.text
    assert "Beta Serviços Ltda" in resposta.json()["resposta"]
    (pergunta,) = assistente.recebidas
    assert pergunta.pergunta == "e a [EMPRESA]?"
    assert "Beta" not in _tudo_que_foi_ao_provedor(assistente)


def test_uma_chamada_ao_provedor_por_pergunta(assistente, empresa, db_session):
    assistente.dados("resumo_do_mes", competencia="2026-09")
    antes = db_session.scalar(
        select(func.count())
        .select_from(UsoAssistente)
        .where(UsoAssistente.conta_id == empresa.conta_id)
    )

    assert _perguntar(empresa.com_empresa).status_code == 200

    assert len(assistente.recebidas) == 1
    db_session.rollback()
    depois = db_session.scalar(
        select(func.count())
        .select_from(UsoAssistente)
        .where(UsoAssistente.conta_id == empresa.conta_id)
    )
    assert depois == antes + 1


def test_log_tem_a_intencao_e_nunca_valores(assistente, empresa, caplog):
    assistente.dados("saldo", competencia="2026-09")

    with caplog.at_level(logging.INFO, logger="app.api.assistente"):
        _perguntar(empresa.com_empresa, "o saldo de R$ 3.200 de setembro?")

    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert "resultado=dados intencao=saldo" in registros
    for secreto in ("3.200", "98.765", "setembro"):
        assert secreto not in registros


# --- regra 2: o assistente nunca grava ----------------------------------------------------


def test_pedido_de_gravar_recebe_recusa_com_a_tela(assistente, empresa, db_session):
    assistente.outro("acao", tela="fechamentos")
    antes = db_session.scalar(select(func.count()).select_from(Fechamento))

    resposta = _perguntar(empresa.com_empresa, "feche o mês de setembro")

    assert resposta.status_code == 200
    assert resposta.json() == {
        "resposta": "Pelo assistente eu só consulto e não gravo nada. "
        "Para isso, use a tela Fechamentos.",
        "respondida": True,
        "links": [{"titulo": "Fechamentos", "caminho": "/fechamentos", "tipo": "tela"}],
    }
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(Fechamento)) == antes


@pytest.mark.parametrize(
    ("tela", "esperado"),
    [
        (
            "equipe",
            {"titulo": "Equipe", "caminho": "/empresas?aba=equipe", "tipo": "tela"},
        ),
        (
            "regras",
            {
                "titulo": "Configurações",
                "caminho": "/ajuda/configuracoes#tolerancia",
                "tipo": "ajuda",
            },
        ),
    ],
)
def test_recusa_aponta_onde_a_tela_existe_no_front(assistente, empresa, tela, esperado):
    # o front não tem /equipe nem /regras: a Equipe é aba de Empresas e a
    # tolerância fica na janela Configurações, que não tem rota
    assistente.outro("acao", tela=tela)

    resposta = _perguntar(empresa.com_empresa, "mude isso")

    assert resposta.json()["links"] == [esperado]


def test_pedido_de_gravar_sem_tela(assistente, empresa):
    assistente.outro("acao")

    resposta = _perguntar(empresa.com_empresa, "apague tudo")

    assert resposta.json()["resposta"] == "Pelo assistente eu só consulto e não gravo nada."
    assert resposta.json()["links"] == []


def test_consulta_que_tenta_gravar_e_barrada_pelo_banco(
    assistente, empresa, db_session, monkeypatch
):
    def responder_gravando(db, *args):
        db.execute(text("CREATE TEMP TABLE assistente_tentou_gravar (x int)"))

    monkeypatch.setattr(consultas, "responder", responder_gravando)
    assistente.dados("resumo_do_mes", competencia="2026-09")

    resposta = _perguntar(empresa.com_empresa)

    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "O assistente está indisponível no momento."}


# --- fora, empresa e flag ----------------------------------------------------------------


def test_fora_do_que_sabe_responder_grava_a_pergunta_marcada(
    assistente, empresa, db_session, apagar_perguntas
):
    assistente.outro("fora")

    resposta = _perguntar(empresa.com_empresa, "compare o pix de R$ 3.200 com outubro")

    assert resposta.json() == {
        "resposta": "Ainda não sei responder isso. Consulte a Central de ajuda.",
        "respondida": False,
        "links": [],
    }
    db_session.rollback()
    gravadas = db_session.scalars(
        select(PerguntaSemResposta.pergunta).where(
            PerguntaSemResposta.pergunta.like("compare o pix%")
        )
    ).all()
    assert gravadas == ["compare o pix de [VALOR_1] com outubro"]


def test_sem_empresa_ativa_responde_so_a_ajuda(assistente, empresa):
    resposta = _perguntar(empresa.sem_empresa, "como envio um extrato?")

    assert resposta.status_code == 200
    (pergunta,) = assistente.recebidas
    assert pergunta.dados is False and pergunta.mes_atual is None


def test_flag_desligada_e_o_assistente_do_adr_020(assistente, empresa, monkeypatch):
    monkeypatch.setattr(settings, "assistente_dados_habilitado", False)

    resposta = _perguntar(empresa.com_empresa, "o pix de R$ 3.200 bateu?")

    assert resposta.status_code == 200
    (pergunta,) = assistente.recebidas
    assert pergunta.dados is False
    # A máscara do ADR-020, sem marcadores.
    assert pergunta.pergunta == "o pix de R$ 3.200 bateu?"


def test_empresa_de_outra_conta_da_404(assistente, empresa, criar_usuario, db_session):
    outra = criar_usuario()
    headers = dict(empresa.com_empresa)
    headers["X-Ledgr-Empresa"] = str(empresa_unica(db_session, outra.conta_id))

    resposta = _perguntar(headers)

    assert resposta.status_code == 404
    assert assistente.recebidas == []


def test_links_de_ajuda_tem_tipo_ajuda(assistente, empresa):
    assistente.saida = RespostaAjuda(
        "1. Clique em Enviar extrato.", True, (("extratos", "enviar-extrato"),), 1, 1, "falso"
    )

    resposta = _perguntar(empresa.sem_empresa, "como envio um extrato?")

    assert resposta.json()["links"][0]["tipo"] == "ajuda"


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        (datetime(2026, 12, 1, 2, 59, tzinfo=UTC), "2026-11"),
        (datetime(2026, 12, 1, 3, 0, tzinfo=UTC), "2026-12"),
        (datetime(2027, 1, 1, 2, 0, tzinfo=UTC), "2026-12"),
    ],
)
def test_mes_atual_e_o_de_sao_paulo(instante, esperado):
    assert mes_atual_de_sao_paulo(instante) == esperado
