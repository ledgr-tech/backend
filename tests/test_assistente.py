"""Assistente da Central de ajuda (issue #137, ADR-020): `POST
/assistente/perguntas`.

Nada sai para a internet: o conteúdo vem de tests/fixtures/ajuda_conteudo.json
por um `httpx.MockTransport`, e o provedor é um falso que guarda o que
recebeu e devolve a resposta combinada no teste.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from app.models import PerguntaSemResposta, UsoAssistente
from app.services.ajuda.conteudo import FonteDaAjuda, obter_fonte_da_ajuda
from app.services.ia.base import ProvedorIAIndisponivel, RespostaAjuda
from app.services.ia.geracao import obter_provedor_dependencia
from main import app

client = TestClient(app)

CONTEUDO = (Path(__file__).parent / "fixtures" / "ajuda_conteudo.json").read_bytes()
URL = "https://ajuda.teste/ajuda/conteudo.json"
INDISPONIVEL = {"detail": "O assistente está indisponível no momento."}
LIMITE = {
    "detail": "Você chegou ao limite de perguntas de hoje. Tente amanhã ou consulte a Central de ajuda."
}


class ProvedorAjudaFalso:
    def __init__(self) -> None:
        self.recebidas = []
        self.identificadores = []
        self.resposta = "1. Clique em Enviar extrato.\n2. Escolha o arquivo."
        self.respondida = True
        self.citacoes: tuple[tuple[str, str], ...] = (("extratos", "enviar-extrato"),)
        self.erro: Exception | None = None

    def responder_ajuda(self, pergunta, *, identificador_anonimo=None):
        self.recebidas.append(pergunta)
        self.identificadores.append(identificador_anonimo)
        if self.erro is not None:
            raise self.erro
        return RespostaAjuda(
            resposta=self.resposta,
            respondida=self.respondida,
            citacoes=self.citacoes,
            tokens_entrada=100,
            tokens_saida=20,
            modelo="falso",
        )


class Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


class Central:
    """O servidor do front de mentira: `status` e `corpo` mudam no teste."""

    def __init__(self) -> None:
        self.status = 200
        self.corpo = CONTEUDO
        self.pedidos = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos += 1
        return httpx.Response(self.status, content=self.corpo)


@pytest.fixture
def assistente(monkeypatch):
    """As duas flags ligadas, o provedor falso e a fonte do conteúdo com o
    fixture. Limites fixos, para não depender do `.env`."""
    monkeypatch.setattr(settings, "llm_habilitado", True)
    monkeypatch.setattr(settings, "assistente_habilitado", True)
    monkeypatch.setattr(settings, "ajuda_conteudo_url", URL)
    monkeypatch.setattr(settings, "assistente_limite_diario_conta", 100)
    monkeypatch.setattr(settings, "assistente_limite_diario_global", 100_000)
    monkeypatch.setattr(settings, "assistente_maximo_conteudo_caracteres", 60000)
    provedor = ProvedorAjudaFalso()
    central = Central()
    relogio = Relogio()
    fonte = FonteDaAjuda(
        url=lambda: settings.ajuda_conteudo_url,
        cliente_http=httpx.Client(transport=httpx.MockTransport(central.handler)),
        relogio=relogio,
    )
    app.dependency_overrides[obter_provedor_dependencia] = lambda: provedor
    app.dependency_overrides[obter_fonte_da_ajuda] = lambda: fonte
    yield provedor, central, relogio
    app.dependency_overrides.pop(obter_provedor_dependencia, None)
    app.dependency_overrides.pop(obter_fonte_da_ajuda, None)


@pytest.fixture
def usuario(db_session, criar_usuario, gerar_token):
    """`usuario(papel)` com os cabeçalhos dele; a conta é a mesma para todos."""
    admin = criar_usuario()

    def _criar(papel="admin"):
        u = admin if papel == "admin" else criar_usuario(conta_id=admin.conta_id, papel=papel)
        return u, cabecalhos(gerar_token(u.conta_id, usuario_id=u.id))

    return _criar


@pytest.fixture
def apagar_perguntas(db_session):
    """As perguntas sem resposta não têm conta: apaga as do teste no fim."""
    antes = datetime.now(UTC)
    yield
    db_session.rollback()
    db_session.execute(delete(PerguntaSemResposta).where(PerguntaSemResposta.criado_em >= antes))
    db_session.commit()


def _perguntar(headers, pergunta="Como envio um extrato?", **extra):
    return client.post(
        "/assistente/perguntas", headers=headers, json={"pergunta": pergunta, **extra}
    )


# --- resposta ------------------------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_os_tres_papeis_perguntam_e_recebem_os_links(db_session, assistente, usuario, papel):
    provedor, _, _ = assistente
    u, headers = usuario(papel)

    resposta = _perguntar({**headers, CABECALHO_EMPRESA: "ignorado"}, tela="extratos")

    assert resposta.status_code == 200, resposta.text
    assert resposta.json() == {
        "resposta": "1. Clique em Enviar extrato.\n2. Escolha o arquivo.",
        "respondida": True,
        "links": [{"titulo": "Enviar um extrato", "caminho": "/ajuda/extratos#enviar-extrato"}],
    }
    (recebida,) = provedor.recebidas
    assert recebida.pergunta == "Como envio um extrato?"
    assert recebida.tela == "extratos"
    assert json.loads(recebida.central)["versao"] == "2026-10-22-teste"
    assert provedor.identificadores == [hashlib.sha256(str(u.conta_id).encode()).hexdigest()]


def test_citacao_inexistente_e_descartada_e_links_limitados_a_tres(db_session, assistente, usuario):
    provedor, _, _ = assistente
    provedor.citacoes = (
        ("extratos", "nao-existe"),
        ("pagina-inventada", "conciliar"),
        ("conciliacao", "conciliar"),
        ("conciliacao", "conciliar"),  # repetida
        ("extratos", "enviar-extrato"),
        ("fechamento", "fechar-mes"),
        ("conciliacao", "justificar"),
    )
    _, headers = usuario()

    links = _perguntar(headers).json()["links"]

    assert links == [
        {"titulo": "Conciliar dois extratos", "caminho": "/ajuda/conciliacao#conciliar"},
        {"titulo": "Enviar um extrato", "caminho": "/ajuda/extratos#enviar-extrato"},
        {"titulo": "Fechar o mês", "caminho": "/ajuda/fechamento#fechar-mes"},
    ]


def test_markdown_alem_dos_passos_sai_da_resposta(db_session, assistente, usuario):
    provedor, _, _ = assistente
    provedor.resposta = "## Como fazer\n1. Clique em **Enviar extrato**.\n2. Use `CSV`."
    _, headers = usuario()

    assert _perguntar(headers).json()["resposta"] == (
        "Como fazer\n1. Clique em Enviar extrato.\n2. Use CSV."
    )


def test_tela_que_nao_existe_no_conteudo_nao_vai_ao_provedor(db_session, assistente, usuario):
    provedor, _, _ = assistente
    _, headers = usuario()

    assert _perguntar(headers, tela="tela-velha").status_code == 200
    assert provedor.recebidas[0].tela is None


def test_historico_vai_mascarado(db_session, assistente, usuario):
    provedor, _, _ = assistente
    _, headers = usuario()
    historico = [
        {"papel": "usuario", "texto": "Meu e-mail é ana@empresa.com.br"},
        {"papel": "assistente", "texto": "Envie o extrato pela tela Extratos."},
    ]

    assert _perguntar(headers, historico=historico).status_code == 200
    turnos = provedor.recebidas[0].historico
    assert [(t.papel, t.texto) for t in turnos] == [
        ("usuario", "Meu e-mail é [EMAIL]"),
        ("assistente", "Envie o extrato pela tela Extratos."),
    ]


# --- perguntas sem resposta e máscara ------------------------------------------------


def test_nao_respondida_grava_so_a_pergunta_mascarada_e_a_tela(
    db_session, assistente, usuario, apagar_perguntas
):
    provedor, _, _ = assistente
    provedor.respondida = False
    provedor.citacoes = ()
    provedor.resposta = "A Central de ajuda não cobre isso."
    _, headers = usuario("analista")
    pergunta = (
        "Meu CPF é 123.456.789-09, o CNPJ é 11.222.333/0001-81 e o e-mail ana@x.com.br: "
        "como emito nota fiscal?"
    )

    resposta = _perguntar(headers, pergunta, tela="conciliacao")

    assert resposta.json()["respondida"] is False
    assert resposta.json()["links"] == []
    esperado = "Meu CPF é [CPF], o CNPJ é [CNPJ] e o e-mail [EMAIL]: como emito nota fiscal?"
    assert provedor.recebidas[0].pergunta == esperado
    gravada = db_session.scalars(
        select(PerguntaSemResposta).order_by(PerguntaSemResposta.criado_em.desc())
    ).first()
    assert (gravada.pergunta, gravada.tela) == (esperado, "conciliacao")
    assert {c.name for c in PerguntaSemResposta.__table__.columns} == {
        "id",
        "pergunta",
        "tela",
        "criado_em",
    }
    for dado in ("123.456.789-09", "11.222.333/0001-81", "ana@x.com.br"):
        assert dado not in gravada.pergunta


def test_respondida_nao_grava_pergunta(db_session, assistente, usuario, apagar_perguntas):
    _, headers = usuario()
    antes = db_session.scalar(select(func.count()).select_from(PerguntaSemResposta))

    _perguntar(headers)

    assert db_session.scalar(select(func.count()).select_from(PerguntaSemResposta)) == antes


def test_limpeza_dos_90_dias_nas_duas_tabelas(db_session, assistente, usuario, apagar_perguntas):
    provedor, _, _ = assistente
    provedor.respondida = False
    u, headers = usuario()
    velho = datetime.now(UTC) - timedelta(days=91)
    recente = datetime.now(UTC) - timedelta(days=89)
    db_session.add_all(
        [
            UsoAssistente(conta_id=u.conta_id, criado_em=velho),
            UsoAssistente(conta_id=u.conta_id, criado_em=recente),
            PerguntaSemResposta(pergunta="velha", criado_em=velho),
            PerguntaSemResposta(pergunta="recente", criado_em=recente),
        ]
    )
    db_session.commit()

    assert _perguntar(headers).status_code == 200

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count()).where(
                UsoAssistente.criado_em < datetime.now(UTC) - timedelta(days=90)
            )
        )
        == 0
    )
    assert (
        db_session.scalar(select(func.count()).where(PerguntaSemResposta.pergunta == "velha")) == 0
    )
    assert (
        db_session.scalar(select(func.count()).where(PerguntaSemResposta.pergunta == "recente"))
        == 1
    )
    assert (
        db_session.scalar(select(func.count()).where(UsoAssistente.conta_id == u.conta_id)) == 2
    )  # o recente e o desta pergunta
    db_session.execute(delete(PerguntaSemResposta).where(PerguntaSemResposta.pergunta == "recente"))
    db_session.commit()


# --- flags e conteúdo ----------------------------------------------------------------


@pytest.mark.parametrize("flag", ["assistente_habilitado", "llm_habilitado"])
def test_flag_desligada_e_503(db_session, assistente, usuario, monkeypatch, flag):
    provedor, central, _ = assistente
    monkeypatch.setattr(settings, flag, False)
    _, headers = usuario()

    resposta = _perguntar(headers)

    assert resposta.status_code == 503
    assert resposta.json() == INDISPONIVEL
    assert provedor.recebidas == [] and central.pedidos == 0


def test_sem_provedor_configurado_e_503(db_session, assistente, usuario):
    app.dependency_overrides[obter_provedor_dependencia] = lambda: None
    _, headers = usuario()

    assert _perguntar(headers).json() == INDISPONIVEL


@pytest.mark.parametrize(
    "falha",
    ["http_500", "json_invalido", "fora_do_formato", "sem_url"],
)
def test_conteudo_indisponivel_sem_copia_e_503(db_session, assistente, usuario, monkeypatch, falha):
    provedor, central, _ = assistente
    if falha == "http_500":
        central.status = 500
    elif falha == "json_invalido":
        central.corpo = b"<html>nao e json</html>"
    elif falha == "fora_do_formato":
        central.corpo = json.dumps({"versao": "x", "paginas": [{"id": "Sem Formato"}]}).encode()
    else:
        monkeypatch.setattr(settings, "ajuda_conteudo_url", "")
    _, headers = usuario()

    resposta = _perguntar(headers)

    assert resposta.status_code == 503
    assert resposta.json() == INDISPONIVEL
    assert provedor.recebidas == []


def test_conteudo_com_copia_antiga_responde_com_ela(db_session, assistente, usuario):
    provedor, central, relogio = assistente
    _, headers = usuario()
    assert _perguntar(headers).status_code == 200

    relogio.agora += 11 * 60  # passou a validade de 10 minutos
    central.status = 500

    assert _perguntar(headers).status_code == 200
    assert central.pedidos == 2
    assert json.loads(provedor.recebidas[-1].central)["versao"] == "2026-10-22-teste"


def test_conteudo_fica_10_minutos_em_cache(db_session, assistente, usuario):
    _, central, relogio = assistente
    _, headers = usuario()

    _perguntar(headers)
    relogio.agora += 9 * 60
    _perguntar(headers)
    assert central.pedidos == 1

    relogio.agora += 2 * 60
    _perguntar(headers)
    assert central.pedidos == 2


def test_conteudo_grande_demais_e_503_com_log_de_erro(
    db_session, assistente, usuario, monkeypatch, caplog
):
    provedor, _, _ = assistente
    monkeypatch.setattr(settings, "assistente_maximo_conteudo_caracteres", 500)
    _, headers = usuario()

    resposta = _perguntar(headers)

    assert resposta.json() == INDISPONIVEL
    assert provedor.recebidas == []
    assert any(
        r.levelname == "ERROR" and "conteudo_grande_demais" in r.getMessage()
        for r in caplog.records
    )


# --- limites -------------------------------------------------------------------------


def test_limite_diario_da_conta(db_session, assistente, usuario, monkeypatch):
    provedor, _, _ = assistente
    monkeypatch.setattr(settings, "assistente_limite_diario_conta", 2)
    _, admin = usuario()
    _, analista = usuario("analista")

    codigos = [_perguntar(h).status_code for h in (admin, analista, admin)]

    assert codigos == [200, 200, 429]
    assert _perguntar(analista).json() == LIMITE
    assert len(provedor.recebidas) == 2


def test_limite_diario_global(
    db_session, assistente, usuario, criar_usuario, gerar_token, monkeypatch
):
    provedor, _, _ = assistente
    hoje = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ja_usadas = db_session.scalar(select(func.count()).where(UsoAssistente.criado_em >= hoje))
    monkeypatch.setattr(settings, "assistente_limite_diario_global", ja_usadas + 1)
    _, headers = usuario()
    outra = criar_usuario()

    assert _perguntar(headers).status_code == 200
    resposta = _perguntar(cabecalhos(gerar_token(outra.conta_id, usuario_id=outra.id)))

    assert resposta.status_code == 429
    assert resposta.json() == LIMITE
    assert len(provedor.recebidas) == 1


def test_rate_limit_por_usuario(db_session, assistente, usuario):
    _, admin = usuario()
    _, gestor = usuario("gestor")

    codigos = [_perguntar(admin).status_code for _ in range(11)]

    assert codigos[:10] == [200] * 10
    assert codigos[10] == 429
    assert _perguntar(gestor).status_code == 200


@pytest.mark.parametrize(
    "erro",
    [
        ProvedorIAIndisponivel("resposta_invalida"),
        ProvedorIAIndisponivel("timeout"),
        RuntimeError(),
    ],
    ids=["resposta_invalida", "timeout", "erro_inesperado"],
)
def test_falha_do_provedor_e_503_sem_contar_uso(db_session, assistente, usuario, erro):
    provedor, _, _ = assistente
    provedor.erro = erro
    u, headers = usuario()

    resposta = _perguntar(headers)

    assert resposta.status_code == 503
    assert resposta.json() == INDISPONIVEL
    assert db_session.scalar(select(func.count()).where(UsoAssistente.conta_id == u.conta_id)) == 0


# --- validação e autenticação ---------------------------------------------------------


@pytest.mark.parametrize(
    "corpo",
    [
        {"pergunta": ""},
        {"pergunta": "   "},
        {"pergunta": "x" * 501},
        {"pergunta": "ok", "historico": [{"papel": "usuario", "texto": "a"}] * 7},
        {"pergunta": "ok", "historico": [{"papel": "usuario", "texto": "a" * 1001}]},
        {"pergunta": "ok", "historico": [{"papel": "sistema", "texto": "a"}]},
        {"pergunta": "ok", "tela": "Tela Com Espaço"},
        {},
    ],
    ids=[
        "vazia",
        "so_espacos",
        "longa",
        "historico_demais",
        "turno_longo",
        "papel_invalido",
        "tela_invalida",
        "sem_pergunta",
    ],
)
def test_corpo_invalido_e_422(db_session, assistente, usuario, corpo):
    provedor, _, _ = assistente
    _, headers = usuario()

    resposta = client.post("/assistente/perguntas", headers=headers, json=corpo)

    assert resposta.status_code == 422
    assert provedor.recebidas == []


def test_pergunta_com_espacos_nas_pontas_e_aparada_e_500_caracteres_passam(
    db_session, assistente, usuario
):
    provedor, _, _ = assistente
    _, headers = usuario()

    assert _perguntar(headers, "  " + "x" * 500 + "  ").status_code == 200
    assert provedor.recebidas[0].pergunta == "x" * 500


def test_sem_token_e_401(assistente):
    assert client.post("/assistente/perguntas", json={"pergunta": "oi"}).status_code == 401


def test_pergunta_e_resposta_nunca_vao_para_o_log(db_session, assistente, usuario, caplog):
    provedor, _, _ = assistente
    provedor.respondida = False
    _, headers = usuario()
    segredo = f"pergunta-secreta-{uuid.uuid4().hex}"

    _perguntar(headers, segredo, historico=[{"papel": "usuario", "texto": segredo}])

    texto_dos_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert segredo not in texto_dos_logs
    assert provedor.resposta not in texto_dos_logs
    assert re.search(r"assistente resultado=nao_respondida", texto_dos_logs)
    db_session.execute(delete(PerguntaSemResposta).where(PerguntaSemResposta.pergunta == segredo))
    db_session.commit()
