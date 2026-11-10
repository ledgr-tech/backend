"""Testes do `POST /explicacoes` (issue #28, ADR-011) contra Postgres real,
com um provedor de IA FALSO via `app.dependency_overrides` — nenhum teste
aqui chama a API real da OpenAI.
"""

import hashlib
import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_conciliacoes import _lancamento_em
from test_ia_contexto import _conciliacao

from app.api.explicacoes import obter_provedor_dependencia
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.database import engine as app_engine
from app.models import Conciliacao, ExplicacaoDivergencia
from app.services.ia import cache as cache_module
from app.services.ia import geracao as geracao_module
from app.services.ia import prompt as prompt_module
from app.services.ia.base import ProvedorIAIndisponivel, RespostaIA
from main import app

client = TestClient(app)


def _existe_transacao_idle() -> bool:
    with app_engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'idle in transaction' AND datname = current_database() "
                "AND pid != pg_backend_pid()"
            )
        ).scalar()
    return n > 0


class ProvedorFalso:
    """Provedor de IA falso pra teste — nunca faz requisição de rede."""

    def __init__(
        self,
        texto: str = "Explicação sintética gerada pelo provedor falso.",
        tokens_entrada: int = 700,
        tokens_saida: int = 42,
        excecao: Exception | None = None,
        verificar_transacao: bool = False,
    ):
        self.chamadas: list[tuple] = []
        self.texto = texto
        self.tokens_entrada = tokens_entrada
        self.tokens_saida = tokens_saida
        self.excecao = excecao
        self.verificar_transacao = verificar_transacao
        self.havia_transacao_aberta: bool | None = None

    def explicar_divergencia(self, contexto, *, identificador_anonimo=None):
        self.chamadas.append((contexto, identificador_anonimo))
        if self.verificar_transacao:
            self.havia_transacao_aberta = _existe_transacao_idle()
        if self.excecao is not None:
            raise self.excecao
        return RespostaIA(
            texto=self.texto,
            tokens_entrada=self.tokens_entrada,
            tokens_saida=self.tokens_saida,
            modelo="gpt-6-luna-falso",
        )


class ProvedorQueApagaConciliacaoDurante:
    """Regressão: apaga a linha de `conciliacoes` numa sessão/conexão
    SEPARADA e comita, de dentro de `explicar_divergencia` — simula uma
    reconciliação concorrente (`POST /conciliacoes` apaga e reescreve as
    linhas do par a cada rodada, ADR-007) acontecendo durante a chamada ao
    provedor. O endpoint não pode 500 nem perder a explicação já gerada."""

    def __init__(self, conciliacao_id: uuid.UUID, texto: str | None = None, excecao=None):
        self.conciliacao_id = conciliacao_id
        self.texto = texto or "Explicação gerada mesmo com a linha já apagada."
        self.excecao = excecao
        self.chamadas: list[tuple] = []

    def explicar_divergencia(self, contexto, *, identificador_anonimo=None):
        self.chamadas.append((contexto, identificador_anonimo))
        sessao_separada = SessionLocal()
        try:
            sessao_separada.query(Conciliacao).filter_by(id=self.conciliacao_id).delete()
            sessao_separada.commit()
        finally:
            sessao_separada.close()

        if self.excecao is not None:
            raise self.excecao
        return RespostaIA(texto=self.texto, tokens_entrada=11, tokens_saida=22, modelo="m")


@pytest.fixture
def provedor_falso():
    fake = ProvedorFalso()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    yield fake
    app.dependency_overrides.pop(obter_provedor_dependencia, None)


@pytest.fixture
def provedor_desabilitado():
    app.dependency_overrides[obter_provedor_dependencia] = lambda: None
    yield
    app.dependency_overrides.pop(obter_provedor_dependencia, None)


def _explicar(headers, conciliacao_id):
    return client.post(
        "/explicacoes", headers=headers, json={"conciliacao_id": str(conciliacao_id)}
    )


@pytest.fixture
def cenario(db_session, criar_empresa, criar_extrato_da_empresa, auth_headers):
    """Empresa + par de extratos + 1 lançamento do banco + 1 do sistema (na
    mesma data, valor diferente) + 1 linha `divergente_valor`."""
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, "Item divergente")
    _lancamento_em(db_session, sistema, "99.00", d, "Candidato mais proximo")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )
    return empresa, linha, auth_headers(empresa.id)


# validações básicas


def test_sem_token_retorna_401():
    assert (
        client.post("/explicacoes", json={"conciliacao_id": str(uuid.uuid4())}).status_code == 401
    )


def test_id_inexistente_retorna_404(criar_empresa, auth_headers):
    empresa = criar_empresa()
    resposta = _explicar(auth_headers(empresa.id), uuid.uuid4())
    assert resposta.status_code == 404
    assert resposta.json()["detail"] == "Conciliação não encontrada."


def test_conciliacao_de_outra_empresa_retorna_404_com_mesma_mensagem(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    outra = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")
    sistema = criar_extrato_da_empresa(dona.id, "sistema")
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session,
        dona.id,
        banco.id,
        sistema.id,
        "sem_correspondencia",
        lancamento_banco_id=item.id,
    )

    de_outra = _explicar(auth_headers(outra.id), linha.id)
    inexistente = _explicar(auth_headers(dona.id), uuid.uuid4())

    assert de_outra.status_code == inexistente.status_code == 404
    assert (
        de_outra.json()["detail"] == inexistente.json()["detail"] == "Conciliação não encontrada."
    )


@pytest.mark.parametrize("status", ["match_exato", "match_tolerancia"])
def test_status_nao_elegivel_retorna_422(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, status
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    item_banco = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    item_sistema = _lancamento_em(db_session, sistema, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        status,
        lancamento_banco_id=item_banco.id,
        lancamento_sistema_id=item_sistema.id,
        regra_aplicada="exato" if status == "match_exato" else "tolerancia",
        score_confianca=Decimal("1.000"),
    )

    resposta = _explicar(auth_headers(empresa.id), linha.id)

    assert resposta.status_code == 422
    assert "não é elegível" in resposta.json()["detail"]


def test_uuid_malformado_retorna_422(criar_empresa, auth_headers):
    empresa = criar_empresa()
    resposta = client.post(
        "/explicacoes", headers=auth_headers(empresa.id), json={"conciliacao_id": "nao-e-um-uuid"}
    )
    assert resposta.status_code == 422


# recurso desligado


def test_recurso_desligado_gera_fallback_sem_chamar_provedor(cenario, provedor_desabilitado):
    _empresa, linha, headers = cenario

    resposta = _explicar(headers, linha.id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["gerada_por_ia"] is False
    assert corpo["em_cache"] is False
    assert corpo["indisponibilidade"] == "desabilitado"
    assert corpo["status"] == "divergente_valor"
    assert corpo["explicacao"]  # texto_deterministico, não vazio


def test_cache_existente_com_recurso_desligado_devolve_fallback_nao_o_cache(
    db_session, cenario, provedor_falso, provedor_desabilitado
):
    empresa, linha, headers = cenario
    # provedor_falso não é usado aqui (só pra garantir que não seria chamado);
    # o override de provedor_desabilitado é o que de fato vale (último a
    # sobrescrever obter_provedor_dependencia).
    db_session.add(
        ExplicacaoDivergencia(
            id=uuid.uuid4(),
            empresa_id=empresa.id,
            chave="a" * 64,
            status="divergente_valor",
            provedor="openai",
            modelo="gpt-6-luna",
            versao_prompt=prompt_module.VERSAO_PROMPT,
            texto="Texto que estava em cache antes de desligar.",
            tokens_entrada=10,
            tokens_saida=10,
        )
    )
    db_session.commit()

    resposta = _explicar(headers, linha.id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["indisponibilidade"] == "desabilitado"
    assert corpo["gerada_por_ia"] is False
    assert corpo["explicacao"] != "Texto que estava em cache antes de desligar."


# sucesso e cache


def test_sucesso_grava_cache_com_tokens_e_nao_grava_contexto(db_session, cenario, provedor_falso):
    empresa, linha, headers = cenario

    resposta = _explicar(headers, linha.id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["gerada_por_ia"] is True
    assert corpo["em_cache"] is False
    assert corpo["indisponibilidade"] is None
    assert corpo["explicacao"] == provedor_falso.texto
    assert len(provedor_falso.chamadas) == 1

    db_session.expire_all()
    linhas_cache = db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).all()
    assert len(linhas_cache) == 1
    gravado = linhas_cache[0]
    assert gravado.texto == provedor_falso.texto
    assert gravado.tokens_entrada == provedor_falso.tokens_entrada
    assert gravado.tokens_saida == provedor_falso.tokens_saida
    assert gravado.status == "divergente_valor"
    assert gravado.provedor == settings.llm_provedor
    assert gravado.modelo == settings.openai_modelo
    assert gravado.versao_prompt == prompt_module.VERSAO_PROMPT
    # nenhuma coluna guarda contexto/descrição — só o texto e os tokens.
    assert set(ExplicacaoDivergencia.__table__.columns.keys()) == {
        "id", "empresa_id", "chave", "status", "provedor", "modelo",
        "versao_prompt", "texto", "tokens_entrada", "tokens_saida",
        "criado_em", "atualizado_em",
    }  # fmt: skip


def test_segunda_chamada_identica_devolve_em_cache_true_e_provedor_chamado_uma_vez(
    cenario, provedor_falso
):
    _empresa, linha, headers = cenario

    primeira = _explicar(headers, linha.id)
    segunda = _explicar(headers, linha.id)

    assert primeira.status_code == segunda.status_code == 200
    assert primeira.json()["em_cache"] is False
    assert segunda.json()["em_cache"] is True
    assert segunda.json()["gerada_por_ia"] is True
    assert segunda.json()["explicacao"] == primeira.json()["explicacao"] == provedor_falso.texto
    assert len(provedor_falso.chamadas) == 1


def test_mesmo_conteudo_em_outra_empresa_gera_de_novo_isolamento(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, provedor_falso
):
    empresas_e_linhas = []
    for _ in range(2):
        empresa = criar_empresa()
        banco = criar_extrato_da_empresa(empresa.id, "banco")
        sistema = criar_extrato_da_empresa(empresa.id, "sistema")
        d = date(2026, 9, 20)
        item = _lancamento_em(db_session, banco, "100.00", d, "Item divergente")
        _lancamento_em(db_session, sistema, "99.00", d, "Candidato mais proximo")
        linha = _conciliacao(
            db_session,
            empresa.id,
            banco.id,
            sistema.id,
            "divergente_valor",
            lancamento_banco_id=item.id,
        )
        empresas_e_linhas.append((empresa, linha))

    for empresa, linha in empresas_e_linhas:
        resposta = _explicar(auth_headers(empresa.id), linha.id)
        assert resposta.status_code == 200
        assert resposta.json()["em_cache"] is False  # nenhuma bateu no cache da outra

    assert len(provedor_falso.chamadas) == 2


def test_mudar_modelo_invalida_cache(cenario, provedor_falso, monkeypatch):
    _empresa, linha, headers = cenario

    primeira = _explicar(headers, linha.id)
    monkeypatch.setattr(settings, "openai_modelo", "gpt-6-luna-outro")
    segunda = _explicar(headers, linha.id)

    assert primeira.json()["em_cache"] is False
    assert segunda.json()["em_cache"] is False
    assert len(provedor_falso.chamadas) == 2


def test_mudar_versao_prompt_invalida_cache(cenario, provedor_falso, monkeypatch):
    _empresa, linha, headers = cenario

    primeira = _explicar(headers, linha.id)
    # cache.py e geracao.py (a regra do POST desde a issue #140) importaram
    # VERSAO_PROMPT por nome ("from ... import VERSAO_PROMPT"), então cada um
    # tem sua própria referência — monkeypatch precisa alcançar as duas, não
    # só app.services.ia.prompt.
    outra_versao = f"{prompt_module.VERSAO_PROMPT}-outra"
    monkeypatch.setattr(cache_module, "VERSAO_PROMPT", outra_versao)
    monkeypatch.setattr(geracao_module, "VERSAO_PROMPT", outra_versao)
    segunda = _explicar(headers, linha.id)

    assert primeira.json()["em_cache"] is False
    assert segunda.json()["em_cache"] is False
    assert len(provedor_falso.chamadas) == 2


# falhas do provedor


@pytest.mark.parametrize(
    "motivo", ["timeout", "http", "resposta_invalida", "nao_configurado", "cota_esgotada"]
)
def test_provedor_indisponivel_gera_fallback_erro_provedor_sem_gravar_cache(
    db_session, cenario, motivo
):
    empresa, linha, headers = cenario
    fake = ProvedorFalso(excecao=ProvedorIAIndisponivel(motivo))
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        resposta = _explicar(headers, linha.id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["indisponibilidade"] == "erro_provedor"
    assert corpo["gerada_por_ia"] is False
    assert corpo["em_cache"] is False

    db_session.expire_all()
    assert db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).count() == 0


def _logs_do_endpoint(caplog, excecao, headers, conciliacao_id):
    fake = ProvedorFalso(excecao=excecao)
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    with caplog.at_level(logging.INFO):
        try:
            resposta = _explicar(headers, conciliacao_id)
        finally:
            app.dependency_overrides.pop(obter_provedor_dependencia, None)
    registros = [r for r in caplog.records if r.name == "app.services.ia.geracao"]
    return resposta, registros


def test_cota_esgotada_devolve_erro_provedor_e_loga_warning(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, caplog
):
    """Issue #87: o contrato com o front não muda, mas o time vê no log."""
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    descricao_secreta = "PIX ENVIADO FULANO-SECRETO-DA-COTA"
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, descricao_secreta)
    _lancamento_em(db_session, sistema, "99.00", d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )

    resposta, registros = _logs_do_endpoint(
        caplog,
        ProvedorIAIndisponivel("cota_esgotada", status_http=429),
        auth_headers(empresa.id),
        linha.id,
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["indisponibilidade"] == "erro_provedor"
    assert corpo["gerada_por_ia"] is False
    assert corpo["explicacao"].startswith("No banco: R$ ")  # texto determinístico
    (registro,) = registros
    assert registro.levelno == logging.WARNING
    assert registro.getMessage() == "explicacoes motivo=cota_esgotada status_http=429"
    assert "FULANO-SECRETO" not in caplog.text
    assert "<dados>" not in caplog.text
    assert "insufficient_quota" not in caplog.text


@pytest.mark.parametrize("motivo", ["timeout", "http", "resposta_invalida", "nao_configurado"])
def test_outros_motivos_continuam_em_info(cenario, caplog, motivo):
    _empresa, linha, headers = cenario

    _resposta, registros = _logs_do_endpoint(
        caplog, ProvedorIAIndisponivel(motivo), headers, linha.id
    )

    (registro,) = registros
    assert registro.levelno == logging.INFO
    assert f"motivo={motivo}" in registro.getMessage()


def test_mudar_modo_de_descricao_invalida_cache(cenario, provedor_falso, monkeypatch):
    """Issue #87: "Item divergente" sai como "[TEXTO]" no modo termos e inteiro
    no mascarada, então a chave muda sozinha."""
    _empresa, linha, headers = cenario

    monkeypatch.setattr(settings, "llm_modo_descricao", "termos")
    primeira = _explicar(headers, linha.id)
    monkeypatch.setattr(settings, "llm_modo_descricao", "mascarada")
    segunda = _explicar(headers, linha.id)
    terceira = _explicar(headers, linha.id)

    assert primeira.json()["em_cache"] is False
    assert segunda.json()["em_cache"] is False
    assert terceira.json()["em_cache"] is True
    assert len(provedor_falso.chamadas) == 2


def test_provedor_indisponivel_proxima_chamada_tenta_de_novo(db_session, cenario):
    _empresa, linha, headers = cenario
    fake_falha = ProvedorFalso(excecao=ProvedorIAIndisponivel("timeout"))
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake_falha
    primeira = _explicar(headers, linha.id)
    app.dependency_overrides.pop(obter_provedor_dependencia)

    fake_sucesso = ProvedorFalso()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake_sucesso
    try:
        segunda = _explicar(headers, linha.id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert primeira.json()["indisponibilidade"] == "erro_provedor"
    assert segunda.status_code == 200
    assert segunda.json()["gerada_por_ia"] is True
    assert segunda.json()["em_cache"] is False
    assert len(fake_sucesso.chamadas) == 1


def test_excecao_inesperada_do_provedor_gera_fallback_erro_provedor(db_session, cenario):
    empresa, linha, headers = cenario
    fake = ProvedorFalso(excecao=RuntimeError("falha de rede simulada"))
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        resposta = _explicar(headers, linha.id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["indisponibilidade"] == "erro_provedor"
    assert corpo["gerada_por_ia"] is False

    db_session.expire_all()
    assert db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).count() == 0


# limites diários


def test_limite_diario_por_empresa(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, monkeypatch
):
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 2)
    monkeypatch.setattr(settings, "llm_limite_diario_global", 1000)
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    headers = auth_headers(empresa.id)
    fake = ProvedorFalso()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake

    try:
        linhas = []
        for indice in range(3):
            d = date(2026, 9, 20 + indice)
            item = _lancamento_em(db_session, banco, f"{100 + indice}.00", d, f"Item {indice}")
            _lancamento_em(db_session, sistema, f"{90 + indice}.00", d, f"Candidato {indice}")
            linhas.append(
                _conciliacao(
                    db_session, empresa.id, banco.id, sistema.id, "divergente_valor",
                    lancamento_banco_id=item.id,
                )
            )  # fmt: skip

        primeira = _explicar(headers, linhas[0].id)
        segunda = _explicar(headers, linhas[1].id)
        terceira = _explicar(headers, linhas[2].id)  # estourou o limite de 2

        assert primeira.json()["indisponibilidade"] is None
        assert segunda.json()["indisponibilidade"] is None
        assert terceira.json()["indisponibilidade"] == "limite_diario"
        assert terceira.json()["gerada_por_ia"] is False
        assert len(fake.chamadas) == 2  # a 3ª não chamou o provedor

        # cache hit continua respondendo normalmente mesmo com o limite estourado
        repete_primeira = _explicar(headers, linhas[0].id)
        assert repete_primeira.json()["em_cache"] is True
        assert repete_primeira.json()["indisponibilidade"] is None
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)


def test_limite_diario_global(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, monkeypatch
):
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 1000)
    monkeypatch.setattr(settings, "llm_limite_diario_global", 2)
    fake = ProvedorFalso()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake

    try:
        linhas = []
        for indice in range(3):
            empresa = criar_empresa()
            banco = criar_extrato_da_empresa(empresa.id, "banco")
            sistema = criar_extrato_da_empresa(empresa.id, "sistema")
            d = date(2026, 9, 20 + indice)
            item = _lancamento_em(db_session, banco, f"{100 + indice}.00", d, f"Item {indice}")
            _lancamento_em(db_session, sistema, f"{90 + indice}.00", d, f"Candidato {indice}")
            linha = _conciliacao(
                db_session, empresa.id, banco.id, sistema.id, "divergente_valor",
                lancamento_banco_id=item.id,
            )  # fmt: skip
            linhas.append((empresa, linha))

        respostas = [_explicar(auth_headers(empresa.id), linha.id) for empresa, linha in linhas]

        assert respostas[0].json()["indisponibilidade"] is None
        assert respostas[1].json()["indisponibilidade"] is None
        assert respostas[2].json()["indisponibilidade"] == "limite_diario"
        assert len(fake.chamadas) == 2
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)


def test_explicacoes_de_ontem_nao_contam_no_limite(
    db_session, cenario, provedor_falso, monkeypatch
):
    empresa, linha, headers = cenario
    monkeypatch.setattr(settings, "llm_limite_diario_conta", 1)
    ontem = datetime_ontem_utc()
    db_session.add(
        ExplicacaoDivergencia(
            id=uuid.uuid4(),
            empresa_id=empresa.id,
            chave="b" * 64,
            status="divergente_valor",
            provedor="openai",
            modelo="gpt-6-luna",
            versao_prompt=prompt_module.VERSAO_PROMPT,
            texto="Explicação de ontem, não deveria contar hoje.",
            tokens_entrada=1,
            tokens_saida=1,
            criado_em=ontem,
        )
    )
    db_session.commit()

    resposta = _explicar(headers, linha.id)

    assert resposta.status_code == 200
    assert resposta.json()["indisponibilidade"] is None
    assert resposta.json()["gerada_por_ia"] is True


def datetime_ontem_utc():
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)


# transação, identificador anônimo, imutabilidade da linha, logs


def test_nenhuma_transacao_aberta_durante_a_chamada_ao_provedor(db_session, cenario):
    _empresa, linha, headers = cenario
    # `linha` foi expirada pelo commit dentro da fixture (expire_on_commit
    # padrão) — materializa o id (dispara o refresh implícito) e fecha essa
    # transação da PRÓPRIA fixture antes de medir, senão o cheque via
    # pg_stat_activity vê essa sessão de setup como "idle in transaction",
    # não a do endpoint (que é o que este teste quer provar).
    linha_id = linha.id
    db_session.rollback()

    fake = ProvedorFalso(verificar_transacao=True)
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        resposta = _explicar(headers, linha_id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    assert fake.havia_transacao_aberta is False


def test_provedor_recebe_identificador_anonimo_igual_ao_sha256_do_empresa_id(
    cenario, provedor_falso
):
    empresa, linha, headers = cenario

    _explicar(headers, linha.id)

    assert len(provedor_falso.chamadas) == 1
    _contexto, identificador = provedor_falso.chamadas[0]
    assert identificador == hashlib.sha256(str(empresa.id).encode()).hexdigest()
    assert identificador != str(empresa.id)


def test_linha_de_conciliacoes_nao_muda(db_session, cenario, provedor_falso):
    _empresa, linha, headers = cenario
    status_antes = linha.status
    banco_id_antes = linha.lancamento_banco_id

    _explicar(headers, linha.id)

    db_session.expire_all()
    recarregada = db_session.get(Conciliacao, linha.id)
    assert recarregada.status == status_antes
    assert recarregada.lancamento_banco_id == banco_id_antes


# regressão: linha de conciliacoes apagada por reconciliação concorrente
# durante a chamada ao provedor (não pode 500)


def test_conciliacao_apagada_durante_chamada_ao_provedor_sucesso_nao_da_500(db_session, cenario):
    _empresa, linha, headers = cenario
    linha_id = linha.id
    status_original = linha.status
    db_session.rollback()  # materializa os atributos acima antes de soltar a sessão de setup

    fake = ProvedorQueApagaConciliacaoDurante(
        linha_id, texto="Explicação gerada com a linha já apagada (sucesso)."
    )
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        resposta = _explicar(headers, linha_id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["gerada_por_ia"] is True
    assert corpo["em_cache"] is False
    assert corpo["indisponibilidade"] is None
    assert corpo["explicacao"] == fake.texto
    assert corpo["status"] == status_original
    assert len(fake.chamadas) == 1


def test_conciliacao_apagada_durante_chamada_ao_provedor_falha_nao_da_500(db_session, cenario):
    _empresa, linha, headers = cenario
    linha_id = linha.id
    status_original = linha.status
    db_session.rollback()

    fake = ProvedorQueApagaConciliacaoDurante(linha_id, excecao=ProvedorIAIndisponivel("timeout"))
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        resposta = _explicar(headers, linha_id)
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["indisponibilidade"] == "erro_provedor"
    assert corpo["gerada_por_ia"] is False
    assert corpo["em_cache"] is False
    assert corpo["status"] == status_original


def test_logs_nunca_contem_descricao_texto_ou_chave(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, caplog
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    descricao_secreta = "DESCRICAO-SINTETICA-NUNCA-DEVE-APARECER-NO-LOG"
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, descricao_secreta)
    _lancamento_em(db_session, sistema, "99.00", d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )
    fake = ProvedorFalso(
        texto="Texto de explicação sintético para o teste de log.",
        excecao=ProvedorIAIndisponivel("http", status_http=500),
    )
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake

    with caplog.at_level(logging.INFO):
        try:
            resposta = _explicar(auth_headers(empresa.id), linha.id)
        finally:
            app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    texto_dos_logs = "\n".join(registro.getMessage() for registro in caplog.records)
    assert descricao_secreta not in texto_dos_logs
    assert fake.texto not in texto_dos_logs
    assert "sk-" not in texto_dos_logs
    assert "motivo=http" in texto_dos_logs
    assert "status_http=500" in texto_dos_logs


def test_log_de_sucesso_nunca_contem_descricao_ou_texto_da_explicacao(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, caplog
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    descricao_secreta = "OUTRA-DESCRICAO-SINTETICA-SECRETA"
    d = date(2026, 9, 20)
    item = _lancamento_em(db_session, banco, "100.00", d, descricao_secreta)
    _lancamento_em(db_session, sistema, "99.00", d, "Candidato")
    linha = _conciliacao(
        db_session,
        empresa.id,
        banco.id,
        sistema.id,
        "divergente_valor",
        lancamento_banco_id=item.id,
    )
    fake = ProvedorFalso(texto="Texto de explicação sintético gerado com sucesso.")
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake

    with caplog.at_level(logging.INFO):
        try:
            resposta = _explicar(auth_headers(empresa.id), linha.id)
        finally:
            app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert resposta.status_code == 200
    texto_dos_logs = "\n".join(registro.getMessage() for registro in caplog.records)
    assert descricao_secreta not in texto_dos_logs
    assert fake.texto not in texto_dos_logs
    assert "sk-" not in texto_dos_logs


# detalhes (fix da explicação detalhada)


def _detalhes(headers, conciliacao_id):
    return client.get(f"/explicacoes/{conciliacao_id}/detalhes", headers=headers)


DETALHES_DO_CENARIO = [
    {"rotulo": "Banco", "valor": "R$ 100,00 em 20/09/2026"},
    {"rotulo": "Sistema", "valor": "R$ 99,00 em 20/09/2026"},
    {"rotulo": "Diferença de valor", "valor": "R$ 1,00 (1,0%)"},
    {"rotulo": "Similaridade das descrições", "valor": "38%"},
]


def _linhas_sem_similaridade(detalhes):
    return [d for d in detalhes if d["rotulo"] != "Similaridade das descrições"]


def test_post_desligado_devolve_detalhes_e_texto_com_numeros(cenario, provedor_desabilitado):
    _empresa, linha, headers = cenario

    corpo = _explicar(headers, linha.id).json()

    assert corpo["indisponibilidade"] == "desabilitado"
    assert corpo["explicacao"] == (
        "No banco: R$ 100,00 em 20/09/2026. No sistema: R$ 99,00 em 20/09/2026. "
        "A data é a mesma e a diferença de valor é de R$ 1,00 (1,0%)."
    )
    assert _linhas_sem_similaridade(corpo["detalhes"]) == _linhas_sem_similaridade(
        DETALHES_DO_CENARIO
    )


def test_post_com_ia_e_com_cache_devolvem_detalhes(cenario, provedor_falso):
    _empresa, linha, headers = cenario

    gerada = _explicar(headers, linha.id).json()
    em_cache = _explicar(headers, linha.id).json()

    assert gerada["gerada_por_ia"] is True and gerada["em_cache"] is False
    assert em_cache["em_cache"] is True
    assert gerada["explicacao"] == provedor_falso.texto  # o texto da IA, não o determinístico
    assert gerada["detalhes"] == em_cache["detalhes"]
    assert gerada["detalhes"][0] == {"rotulo": "Banco", "valor": "R$ 100,00 em 20/09/2026"}


def test_post_erro_e_limite_devolvem_detalhes(cenario, monkeypatch):
    _empresa, linha, headers = cenario
    fake = ProvedorFalso(excecao=ProvedorIAIndisponivel("timeout"))
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        erro = _explicar(headers, linha.id).json()
        monkeypatch.setattr(settings, "llm_limite_diario_global", 0)
        limite = _explicar(headers, linha.id).json()
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert erro["indisponibilidade"] == "erro_provedor"
    assert limite["indisponibilidade"] == "limite_diario"
    for corpo in (erro, limite):
        assert corpo["explicacao"].startswith("No banco: R$ 100,00 em 20/09/2026.")
        assert corpo["detalhes"][0] == {"rotulo": "Banco", "valor": "R$ 100,00 em 20/09/2026"}


@pytest.mark.parametrize(
    "texto",
    [
        "O sistema lançou R$ 98,00 na mesma data.",
        "O lançamento do banco é de 21/09/2026.",
        "Com tolerância de 3 dias o par fecharia.",
    ],
)
def test_texto_da_ia_com_numero_inventado_e_descartado(db_session, cenario, caplog, texto):
    empresa, linha, headers = cenario
    fake = ProvedorFalso(texto=texto)
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        with caplog.at_level(logging.INFO, logger="app.services.ia.geracao"):
            primeira = _explicar(headers, linha.id).json()
        segunda = _explicar(headers, linha.id).json()
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert primeira["indisponibilidade"] == "erro_provedor"
    assert primeira["gerada_por_ia"] is False
    assert primeira["explicacao"].startswith("No banco: R$ 100,00")
    assert primeira["detalhes"]
    assert len(fake.chamadas) == 2  # não entrou no cache: a segunda chama de novo
    assert segunda["indisponibilidade"] == "erro_provedor"
    db_session.expire_all()
    assert db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).count() == 0
    assert db_session.get(Conciliacao, linha.id).explicacao_chave is None
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "explicacoes texto_rejeitado" in logs
    assert texto not in logs


def test_texto_da_ia_que_copia_os_fatos_e_aceito(db_session, cenario):
    empresa, linha, headers = cenario
    fake = ProvedorFalso(
        texto=(
            "No banco há R$ 100,00 em 20/09/2026 e no sistema R$ 99,00 na mesma data, uma "
            "diferença de R$ 1,00 (1,0%). Pode ser um desconto ou uma tarifa descontada. "
            "Confira o lançamento ou justifique a divergência."
        )
    )
    app.dependency_overrides[obter_provedor_dependencia] = lambda: fake
    try:
        corpo = _explicar(headers, linha.id).json()
    finally:
        app.dependency_overrides.pop(obter_provedor_dependencia, None)

    assert corpo["gerada_por_ia"] is True
    assert corpo["explicacao"] == fake.texto
    db_session.expire_all()
    assert db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).count() == 1


def test_get_detalhes_sem_token_retorna_401():
    assert client.get(f"/explicacoes/{uuid.uuid4()}/detalhes").status_code == 401


def test_get_detalhes_nao_chama_provedor_nem_grava(
    db_session, cenario, provedor_falso, monkeypatch
):
    monkeypatch.setattr(settings, "llm_habilitado", True)
    empresa, linha, headers = cenario

    resposta = _detalhes(headers, linha.id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo == {
        "conciliacao_id": str(linha.id),
        "status": "divergente_valor",
        "texto": (
            "No banco: R$ 100,00 em 20/09/2026. No sistema: R$ 99,00 em 20/09/2026. "
            "A data é a mesma e a diferença de valor é de R$ 1,00 (1,0%)."
        ),
        "detalhes": corpo["detalhes"],
    }
    assert _linhas_sem_similaridade(corpo["detalhes"]) == _linhas_sem_similaridade(
        DETALHES_DO_CENARIO
    )
    assert provedor_falso.chamadas == []
    db_session.expire_all()
    assert db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa.id).count() == 0
    assert db_session.get(Conciliacao, linha.id).explicacao_chave is None


def test_get_detalhes_funciona_com_ia_desligada_e_com_limite_estourado(
    cenario, provedor_desabilitado, monkeypatch
):
    monkeypatch.setattr(settings, "llm_limite_diario_global", 0)
    _empresa, linha, headers = cenario
    assert _detalhes(headers, linha.id).status_code == 200


@pytest.mark.parametrize(
    ("status", "esperado"),
    [
        ("divergente_data", "O valor é o mesmo e o sistema lançou 2 dias depois do banco."),
        ("divergente_valor", "a diferença de valor é de R$ 0,50"),
        ("sem_correspondencia", "Nenhum lançamento do sistema que ficou sem par"),
        ("duplicado", "Os pares possíveis já foram formados"),
        ("tarifa_bancaria", "termo conhecido de tarifa bancária"),
    ],
)
def test_get_detalhes_200_por_status(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, status, esperado
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    d = date(2026, 9, 21)
    item = _lancamento_em(db_session, banco, "-10.00", d, "TARIFA PACOTE")
    outro = None
    if status == "divergente_data":
        outro = _lancamento_em(db_session, sistema, "-10.00", d + timedelta(days=2), "x")
    elif status == "divergente_valor":
        _lancamento_em(db_session, sistema, "-9.50", d, "x")
    elif status == "duplicado":
        _lancamento_em(db_session, banco, "-10.00", d, "y")
        _lancamento_em(db_session, sistema, "-10.00", d, "z")
    linha = _conciliacao(
        db_session, empresa.id, banco.id, sistema.id, status, item.id, outro.id if outro else None
    )

    resposta = _detalhes(auth_headers(empresa.id), linha.id)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == status
    assert esperado in corpo["texto"]
    assert corpo["detalhes"][0] == {"rotulo": "Banco", "valor": "-R$ 10,00 em 21/09/2026"}
    assert "TARIFA PACOTE" not in resposta.text


def test_get_detalhes_404_de_outra_empresa_mesmo_da_mesma_conta(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    dona = criar_empresa()
    mesma_conta = criar_empresa(conta_id=dona.conta_id)
    outra_conta = criar_empresa()
    banco = criar_extrato_da_empresa(dona.id, "banco")
    sistema = criar_extrato_da_empresa(dona.id, "sistema")
    item = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    linha = _conciliacao(db_session, dona.id, banco.id, sistema.id, "sem_correspondencia", item.id)

    respostas = [
        _detalhes(auth_headers(mesma_conta.id), linha.id),
        _detalhes(auth_headers(outra_conta.id), linha.id),
        _detalhes(auth_headers(dona.id), uuid.uuid4()),
    ]

    assert [r.status_code for r in respostas] == [404, 404, 404]
    assert {r.json()["detail"] for r in respostas} == {"Conciliação não encontrada."}
    assert _detalhes(auth_headers(dona.id), linha.id).status_code == 200


@pytest.mark.parametrize("status", ["match_exato", "match_tolerancia", "match_agrupado"])
def test_get_detalhes_422_para_match(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, status
):
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    item_banco = _lancamento_em(db_session, banco, "10.00", date(2026, 9, 20), "Item")
    item_sistema = _lancamento_em(db_session, sistema, "10.00", date(2026, 9, 20), "Item")
    linha = Conciliacao(
        empresa_id=empresa.id,
        extrato_banco_id=banco.id,
        extrato_sistema_id=sistema.id,
        lancamento_banco_id=item_banco.id,
        lancamento_sistema_id=None if status == "match_agrupado" else item_sistema.id,
        status=status,
        regra_aplicada={"match_exato": "exato", "match_tolerancia": "tolerancia"}.get(
            status, "agrupamento"
        ),
        score_confianca=Decimal("1.000"),
        grupo_id=uuid.uuid4() if status == "match_agrupado" else None,
    )
    db_session.add(linha)
    db_session.commit()

    resposta = _detalhes(auth_headers(empresa.id), linha.id)

    assert resposta.status_code == 422
    assert "não é elegível" in resposta.json()["detail"]


def test_get_detalhes_uuid_malformado_retorna_422(criar_empresa, auth_headers):
    empresa = criar_empresa()
    assert _detalhes(auth_headers(empresa.id), "nao-e-um-uuid").status_code == 422
