"""Testes unitários da camada de normalização (issue #12) — chamam
`normalizar_extrato` direto, sem passar pelo endpoint HTTP.

Precisam de Postgres real (a função grava Lancamento e atualiza Extrato de
verdade): usam a fixture `db_session` de tests/conftest.py, que pula estes
testes quando não há Postgres real disponível (CI hoje — ver comentário lá
e em .github/workflows/ci.yml).

Cobertura, mapeada nos nomes:
- OFX válido: test_normalizar_extrato_ofx_valido_gera_lancamentos_e_status_concluido
- CSV válido: test_normalizar_extrato_csv_valido_gera_lancamentos_e_status_concluido
- Conteúdo corrompido (ofx e csv, sem reduzir o rigor da Sprint 1):
  test_normalizar_extrato_ofx_corrompido_seta_status_erro_sem_inserir_lancamento
  test_normalizar_extrato_csv_corrompido_seta_status_erro_sem_inserir_lancamento
- Reprocessamento idempotente (ON CONFLICT DO NOTHING, ADR-004):
  test_normalizar_extrato_reprocessado_nao_duplica_lancamento
"""

from pathlib import Path

from app.models import Extrato, Lancamento
from app.services.normalizacao import normalizar_extrato

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_normalizar_extrato_ofx_valido_gera_lancamentos_e_status_concluido(
    db_session, criar_extrato
):
    extrato = criar_extrato("ofx")
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    normalizar_extrato(extrato.id, "ofx", conteudo)

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "concluido"
    assert extrato_atualizado.quantidade_lancamentos == 2

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 2
    assert {lancamento.tipo for lancamento in lancamentos} == {"credito", "debito"}


def test_normalizar_extrato_csv_valido_gera_lancamentos_e_status_concluido(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")
    conteudo = (FIXTURES_DIR / "extrato_valido.csv").read_bytes()

    normalizar_extrato(extrato.id, "csv", conteudo)

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "concluido"
    assert extrato_atualizado.quantidade_lancamentos == 3

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 3


def test_normalizar_extrato_ofx_corrompido_seta_status_erro_sem_inserir_lancamento(
    db_session, criar_extrato
):
    extrato = criar_extrato("ofx")

    normalizar_extrato(extrato.id, "ofx", b"isso claramente nao eh um arquivo ofx valido")

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "erro"
    assert extrato_atualizado.quantidade_lancamentos is None

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert lancamentos == []


def test_normalizar_extrato_csv_corrompido_seta_status_erro_sem_inserir_lancamento(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")

    normalizar_extrato(extrato.id, "csv", b"isso claramente nao eh um csv de extrato valido")

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "erro"
    assert extrato_atualizado.quantidade_lancamentos is None

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert lancamentos == []


def test_normalizar_extrato_reprocessado_nao_duplica_lancamento(db_session, criar_extrato):
    extrato = criar_extrato("ofx")
    conteudo = (FIXTURES_DIR / "extrato_valido.ofx").read_bytes()

    normalizar_extrato(extrato.id, "ofx", conteudo)
    normalizar_extrato(extrato.id, "ofx", conteudo)  # reprocessa o mesmo extrato

    db_session.expire_all()
    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 2  # ON CONFLICT DO NOTHING (ADR-004) — não duplicou

    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "concluido"
    assert extrato_atualizado.quantidade_lancamentos == 2
