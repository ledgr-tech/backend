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

Nunca faz TRUNCATE/DELETE indiscriminado nas tabelas — `db_session` pode
apontar pro Postgres compartilhado de desenvolvimento (Railway, ver .env);
cada fábrica limpa só as linhas que ela mesma criou.
"""

import hashlib
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

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.rate_limit import limiter
from app.models import (
    Conciliacao,
    Configuracao,
    Empresa,
    ExecucaoConciliacao,
    Extrato,
    Lancamento,
    LinhaInvalida,
)

RAIZ = Path(__file__).resolve().parent.parent
SECRET_TESTE = "secret-de-teste-nao-usar-em-producao"


@pytest.fixture(autouse=True)
def _auth_e_rate_limit_de_teste(monkeypatch):
    """Secret fixo pros tokens gerados nos testes (independe do .env/CI) e
    contador do rate limit zerado entre testes (é por IP, em memória)."""
    monkeypatch.setattr(settings, "nextauth_secret", SECRET_TESTE)
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def gerar_token():
    """Simula o JWT que o NextAuth emite (contrato em app/core/auth.py)."""

    def _gerar(
        empresa_id: uuid.UUID | str | None,
        secret: str = SECRET_TESTE,
        expira_em: timedelta = timedelta(days=7),
    ) -> str:
        agora = datetime.now(UTC)
        claims = {
            "sub": str(uuid.uuid4()),
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
    # CNPJ de teste: 14 dígitos aleatórios, sem validação de dígito
    # verificador (o schema só exige VARCHAR(14) — ver app/models/empresa.py).
    return str(random.randint(10**13, 10**14 - 1))


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
        # Configuracao é 1:1 com Empresa (não passa por Extrato) — apagar
        # antes da Empresa, senão a FK configuracoes_empresa_id_fkey barra o
        # delete (issue #22 foi o primeiro teste a gravar Configuracao aqui).
        db_session.query(Configuracao).filter_by(empresa_id=empresa_id).delete()
        db_session.query(Empresa).filter_by(id=empresa_id).delete()
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
