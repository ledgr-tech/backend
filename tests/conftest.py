"""Fixtures compartilhadas de teste.

Testes que gravam no banco (issue #12 em diante — normalização grava
lançamento de verdade) usam a fixture `db_session` abaixo, e as fábricas
`criar_empresa`/`criar_extrato` pra montar dado de teste. Em CI, o
DATABASE_URL configurado (.github/workflows/ci.yml) aponta pra um Postgres
que não existe de verdade — decisão já registrada lá: "Integração contra
Postgres real de CI é escopo da Sprint 6" (ver
07-tecnico/backlog-de-sprints-do-mvp.md no vault). Por isso `db_session`
PULA (não falha) o teste que depende dela quando não consegue conectar, em
vez de quebrar a CI antes da hora — mesmo raciocínio já registrado no
comentário do ci.yml, só aplicado agora que a issue #12 precisa de banco de
verdade pra testar. Localmente, com um DATABASE_URL de Postgres real (ver
.env), esses testes rodam de verdade.

Nunca faz TRUNCATE/DELETE indiscriminado nas tabelas — `db_session` pode
apontar pro Postgres compartilhado de desenvolvimento (Railway, ver .env);
cada fábrica limpa só as linhas que ela mesma criou.
"""

import random
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.database import SessionLocal, engine
from app.models import Empresa, Extrato, Lancamento


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
            "Postgres real não disponível — DATABASE_URL de CI aponta pra um banco "
            "que não existe de verdade (ver comentário em .github/workflows/ci.yml). "
            "Integração contra Postgres real de CI é escopo da Sprint 6."
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
    o que essa fábrica criou: a empresa e qualquer extrato/lançamento
    gravado contra ela durante o teste — inclusive via HTTP, não só pelos
    outros helpers deste arquivo."""
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
            db_session.query(Lancamento).filter(Lancamento.extrato_id.in_(extrato_ids)).delete(
                synchronize_session=False
            )
            db_session.query(Extrato).filter(Extrato.id.in_(extrato_ids)).delete(
                synchronize_session=False
            )
        db_session.query(Empresa).filter_by(id=empresa_id).delete()
    db_session.commit()


@pytest.fixture
def criar_extrato(db_session, criar_empresa):
    """Fábrica de Extrato real no banco, associado a uma Empresa nova criada
    pra ele. Limpeza fica a cargo de `criar_empresa` (cascata: lançamento ->
    extrato -> empresa)."""

    def _criar(formato: str, tamanho_bytes: int = 0) -> Extrato:
        empresa = criar_empresa()
        extrato = Extrato(
            empresa_id=empresa.id,
            nome_arquivo=f"extrato_teste.{formato}",
            formato=formato,
            tamanho_bytes=tamanho_bytes,
            status="pendente",
        )
        db_session.add(extrato)
        db_session.commit()
        return extrato

    return _criar
