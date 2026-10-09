"""Fixtures compartilhadas de teste.

Testes que gravam no banco (issue #12 em diante — normalização grava
lançamento de verdade) usam a fixture `db_session` abaixo, e as fábricas
`criar_empresa`/`criar_extrato` pra montar dado de teste. A CI roda um
service container de Postgres real desde a issue #12 (antecipado do
Sprint 6 — ver .github/workflows/ci.yml: `ON CONFLICT DO NOTHING` é
sintaxe de dialeto Postgres, não dava pra validar sem banco de verdade).
`db_session` PULA (não falha) o teste que depende dela quando não consegue
conectar — isso é só um fallback pra rodar a suíte localmente sem Postgres
configurado (ex: só editando parser, sem tocar em normalização); em CI, com
o service container sempre disponível, esses testes rodam de verdade, não
aparecem como skipped.

Desde a issue #65 a suíte NUNCA roda contra banco remoto: `pytest_configure`
abaixo encerra tudo antes do primeiro teste se DATABASE_URL não apontar pra
localhost. Não há variável de escape — teste grava e apaga linha, e contra o
banco do Railway isso é perda de dado, não inconveniente. Rode o Postgres em
Docker (o comando está em 07-tecnico/stack-e-deploy.md, no vault).

Mesmo assim, nunca faz TRUNCATE/DELETE indiscriminado nas tabelas: cada fábrica
limpa só as linhas que ela mesma criou. O banco local pode ser o mesmo que você
usa pra desenvolver, com dado que você quer manter.
"""

import hashlib
import logging
import os
import random
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jwt
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core import bloqueio_login, senha
from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.cnpj import digito_verificador
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.rate_limit import limiter
from app.models import (
    Conciliacao,
    Configuracao,
    DecisaoLinha,
    Empresa,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    Usuario,
)
from app.services.email import MensagemEmail

RAIZ = Path(__file__).resolve().parent.parent
SECRET_TESTE = "secret-de-teste-nao-usar-em-producao"
SENHA_USUARIO_TESTE = "senha-atual-123"
FRONTEND_URL_TESTE = "https://app.ledgr.com.br"


def pytest_configure(config):
    """Encerra a suíte antes do primeiro teste se DATABASE_URL não for local.

    É a primeira coisa que roda, de propósito: as fábricas deste arquivo criam e
    apagam empresa, usuário, extrato e lançamento de verdade, e o `.env` do
    repositório aponta DATABASE_URL pro Railway. Sem esta guarda, um `pytest`
    rodado sem pensar grava e apaga linha no banco de produção.

    Não existe variável de escape, ao contrário do `migrations/env.py` (que o
    pre-deploy do Railway precisa destravar): nenhuma situação justifica rodar
    esta suíte contra banco remoto.
    """
    try:
        exigir_banco_local(settings.database_url)
    except BancoNaoLocal as erro:
        pytest.exit(
            f"Suíte interrompida: {erro}\n\n"
            "A suíte cria e apaga linha de verdade, então só roda contra Postgres "
            "local. Suba um em Docker e aponte DATABASE_URL pra ele, por exemplo:\n\n"
            "  docker run -d --name ledgr-dev -p 127.0.0.1:55432:5432 \\\n"
            "    -e POSTGRES_USER=ledgr -e POSTGRES_PASSWORD=ledgr \\\n"
            "    -e POSTGRES_DB=ledgr postgres:18\n\n"
            "  DATABASE_URL=postgresql+psycopg://ledgr:ledgr@127.0.0.1:55432/ledgr\n\n"
            "O roteiro completo está em 07-tecnico/stack-e-deploy.md, no vault.",
            returncode=1,
        )


@pytest.fixture(autouse=True)
def _auth_e_rate_limit_de_teste(monkeypatch):
    """Secret fixo pros tokens gerados nos testes (independe do .env/CI) e
    contador do rate limit zerado entre testes (é por IP, em memória)."""
    monkeypatch.setattr(settings, "nextauth_secret", SECRET_TESTE)
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _bloqueio_de_login_zerado():
    """O bloqueio de login por conta (issue #67) é em memória e vale para o
    processo inteiro: sem zerar, as falhas de um teste bloqueariam o e-mail de
    outro."""
    bloqueio_login.limpar_tudo()
    yield
    bloqueio_login.limpar_tudo()


@pytest.fixture(autouse=True)
def _logs_de_app_chegam_ao_caplog(monkeypatch):
    """O `main` desliga a propagação do logger `app` para a linha não sair
    duplicada (app/core/logging.py, issue #102). O `caplog` lê do logger raiz,
    então nos testes a propagação volta a valer."""
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)


@pytest.fixture
def gerar_token():
    """Simula o JWT que o NextAuth emite (contrato em app/core/auth.py)."""

    def _gerar(
        empresa_id: uuid.UUID | str | None,
        secret: str = SECRET_TESTE,
        expira_em: timedelta = timedelta(days=7),
        usuario_id: uuid.UUID | str | None = None,
    ) -> str:
        agora = datetime.now(UTC)
        claims = {
            # `sub` aleatório basta pras rotas que só leem `empresa_id`; as
            # rotas por usuário (issue #66) precisam do id de um usuário real.
            "sub": str(usuario_id or uuid.uuid4()),
            "email": "usuario@teste.com",
            "iat": agora,
            "exp": agora + expira_em,
        }
        if empresa_id is not None:
            claims["empresa_id"] = str(empresa_id)
        return jwt.encode(claims, secret, algorithm="HS256")

    return _gerar


@pytest.fixture
def auth_headers(gerar_token):
    def _headers(empresa_id: uuid.UUID | str) -> dict[str, str]:
        return {"Authorization": f"Bearer {gerar_token(empresa_id)}"}

    return _headers


def _postgres_disponivel() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False


@pytest.fixture(scope="session")
def postgres_disponivel() -> bool:
    return _postgres_disponivel()


@pytest.fixture
def db_session(postgres_disponivel):
    if not postgres_disponivel:
        pytest.skip(
            "Postgres real não disponível — fallback pra rodar a suíte localmente "
            "sem banco configurado. Em CI, o service container do "
            ".github/workflows/ci.yml garante que este teste roda de verdade, não "
            "pula (ver comentário lá)."
        )
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _gerar_cnpj() -> str:
    """CNPJ fictício com dígitos verificadores válidos (passa no /register)."""
    base = f"{random.randint(0, 10**8 - 1):08d}0001"
    base += digito_verificador(base)
    return base + digito_verificador(base)


@pytest.fixture
def gerar_cnpj():
    return _gerar_cnpj


@pytest.fixture
def criar_empresa(db_session):
    """Fábrica de Empresa real no banco. A limpeza no fim do teste apaga só
    o que essa fábrica criou: a empresa e qualquer extrato/lançamento/linha
    inválida/configuração/execução de conciliação gravado contra ela durante
    o teste — inclusive via HTTP, não só pelos outros helpers deste arquivo."""
    empresas_criadas: list[uuid.UUID] = []

    def _criar(razao_social: str = "Empresa de teste") -> Empresa:
        empresa = Empresa(razao_social=razao_social, cnpj=_gerar_cnpj())
        db_session.add(empresa)
        db_session.commit()
        empresas_criadas.append(empresa.id)
        return empresa

    yield _criar

    for empresa_id in empresas_criadas:
        # Eventos de decisão (issue #83) têm FK pra extratos e usuarios.
        db_session.query(DecisaoLinha).filter_by(empresa_id=empresa_id).delete()
        db_session.query(Fechamento).filter_by(empresa_id=empresa_id).delete()
        extrato_ids = [
            row.id for row in db_session.query(Extrato.id).filter_by(empresa_id=empresa_id).all()
        ]
        if extrato_ids:
            # Antes de lançamentos/extratos, por causa das FKs de
            # conciliacoes/execucoes_conciliacao (issue #27, ADR-010).
            db_session.query(Conciliacao).filter(
                Conciliacao.extrato_banco_id.in_(extrato_ids)
                | Conciliacao.extrato_sistema_id.in_(extrato_ids)
            ).delete(synchronize_session=False)
            db_session.query(ExecucaoConciliacao).filter(
                ExecucaoConciliacao.extrato_banco_id.in_(extrato_ids)
                | ExecucaoConciliacao.extrato_sistema_id.in_(extrato_ids)
            ).delete(synchronize_session=False)
            db_session.query(Lancamento).filter(Lancamento.extrato_id.in_(extrato_ids)).delete(
                synchronize_session=False
            )
            db_session.query(LinhaInvalida).filter(
                LinhaInvalida.extrato_id.in_(extrato_ids)
            ).delete(synchronize_session=False)
            db_session.query(Extrato).filter(Extrato.id.in_(extrato_ids)).delete(
                synchronize_session=False
            )
        # Configuracao e ExplicacaoDivergencia são FK direta de Empresa (não
        # passam por Extrato) — apagar antes da Empresa, senão a FK barra o
        # delete (issue #22 foi o primeiro teste a gravar Configuracao aqui;
        # issue #28/ADR-011 acrescenta o cache de explicações).
        db_session.query(Configuracao).filter_by(empresa_id=empresa_id).delete()
        db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa_id).delete()
        db_session.query(Empresa).filter_by(id=empresa_id).delete()
    db_session.commit()


class ProvedorEmailFalso:
    """Guarda as mensagens em vez de chamar o Resend (issue #66). Com
    `falha`, levanta essa exceção em todo envio."""

    def __init__(self, falha: Exception | None = None) -> None:
        self.enviadas: list[MensagemEmail] = []
        self._falha = falha

    def enviar(self, mensagem: MensagemEmail) -> None:
        if self._falha is not None:
            raise self._falha
        self.enviadas.append(mensagem)


@pytest.fixture
def provedor(monkeypatch):
    """Troca o provedor de e-mail das rotas por um `ProvedorEmailFalso` e
    fixa `FRONTEND_URL`, que o envio exige."""
    from app.api.senha import obter_provedor_email_dependencia
    from main import app

    falso = ProvedorEmailFalso()
    app.dependency_overrides[obter_provedor_email_dependencia] = lambda: falso
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)
    yield falso
    app.dependency_overrides.pop(obter_provedor_email_dependencia, None)


@pytest.fixture
def criar_usuario(db_session, criar_empresa):
    """Usuário real no banco, numa empresa nova, com a senha
    `SENHA_USUARIO_TESTE` (ou sem senha, como conta do Google). Apaga só os
    que criou, antes da limpeza de `criar_empresa` (os tokens de
    `tokens_email` caem junto, pelo ON DELETE CASCADE)."""
    criados: list[uuid.UUID] = []

    def _criar(com_senha: bool = True) -> Usuario:
        empresa = criar_empresa()
        usuario = Usuario(
            empresa_id=empresa.id,
            nome="Maria Financeiro",
            email=f"{uuid.uuid4().hex[:12]}@teste.com",
            senha_hash=senha.gerar_hash(SENHA_USUARIO_TESTE) if com_senha else None,
            google_sub=None if com_senha else f"google-{uuid.uuid4().hex}",
        )
        db_session.add(usuario)
        db_session.commit()
        criados.append(usuario.id)
        return usuario

    yield _criar

    db_session.rollback()
    db_session.query(DecisaoLinha).filter(DecisaoLinha.usuario_id.in_(criados)).delete(
        synchronize_session=False
    )
    db_session.query(Usuario).filter(Usuario.id.in_(criados)).delete(synchronize_session=False)
    db_session.commit()


@pytest.fixture
def criar_extrato(db_session, criar_empresa):
    """Fábrica de Extrato real no banco, associado a uma Empresa nova criada
    pra ele. Limpeza fica a cargo de `criar_empresa` (cascata: lançamento ->
    extrato -> empresa)."""

    def _criar(formato: str, tamanho_bytes: int = 0, origem: str = "banco") -> Extrato:
        empresa = criar_empresa()
        extrato = Extrato(
            empresa_id=empresa.id,
            nome_arquivo=f"extrato_teste.{formato}",
            formato=formato,
            tamanho_bytes=tamanho_bytes,
            origem=origem,
            status="pendente",
        )
        db_session.add(extrato)
        db_session.commit()
        return extrato

    return _criar


@pytest.fixture
def criar_extrato_da_empresa(db_session):
    """Extrato com status/origem à escolha numa empresa já existente (ao
    contrário de `criar_extrato` acima, que cria uma empresa nova a cada
    chamada) — usado pelos testes de POST/GET/exportação de conciliações
    (tests/test_conciliacoes.py, tests/test_conciliacoes_exportacao.py)."""

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
    """Lançamentos em lote, sempre na mesma data (2026-09-05) — usado pelos
    mesmos testes que `criar_extrato_da_empresa` acima."""

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


# Migrações: banco descartável no mesmo servidor, nunca o de desenvolvimento.
@pytest.fixture
def url_banco_descartavel(postgres_disponivel):
    if not postgres_disponivel:
        pytest.skip("Postgres real não disponível")
    nome = f"ledgr_mig_{uuid.uuid4().hex[:8]}"
    base = make_url(settings.database_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{nome}"'))
    except (OperationalError, ProgrammingError):
        admin.dispose()
        pytest.skip("Sem permissão pra criar banco descartável")
    try:
        yield base.set(database=nome)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{nome}" WITH (FORCE)'))
        admin.dispose()


def alembic_cli(url, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=RAIZ,
        env=env,
        check=True,
        capture_output=True,
    )
