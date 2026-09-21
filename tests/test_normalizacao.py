"""Testes unitários da camada de normalização (issue #12) — chamam
`normalizar_extrato` direto, sem passar pelo endpoint HTTP.

Precisam de Postgres real (a função grava Lancamento e atualiza Extrato de
verdade): usam a fixture `db_session` de tests/conftest.py, que pula estes
testes quando não há Postgres real disponível (CI hoje — ver comentário lá
e em .github/workflows/ci.yml).

Cobertura, mapeada nos nomes:
- OFX válido: test_normalizar_extrato_ofx_valido_gera_lancamentos_e_status_concluido
- CSV válido: test_normalizar_extrato_csv_valido_gera_lancamentos_e_status_concluido
- Conteúdo corrompido / erro estrutural (ofx e csv, sem reduzir o rigor da
  Sprint 1; status="erro" sem registrar linhas_invalidas — ver issue #13):
  test_normalizar_extrato_ofx_corrompido_seta_status_erro_sem_inserir_lancamento
  test_normalizar_extrato_csv_corrompido_seta_status_erro_sem_inserir_lancamento
- Reprocessamento idempotente (ON CONFLICT DO NOTHING, ADR-004):
  test_normalizar_extrato_reprocessado_nao_duplica_lancamento
- Relatório de validação de linhas, issue #13:
  test_normalizar_extrato_csv_linha_invalida_no_meio_status_concluido_com_erros
  test_normalizar_extrato_csv_todas_linhas_invalidas_status_erro_mas_persiste_linhas_invalidas
  test_normalizar_extrato_ofx_transacao_malformada_tolera_e_marca_concluido_com_erros
"""

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.models import Extrato, Lancamento, LinhaInvalida
from app.parsers.tipos import ErroLinha, LancamentoNormalizado, ResultadoParsing
from app.services.normalizacao import _calcular_hash_dedup, normalizar_extrato

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


def test_normalizar_extrato_csv_linha_invalida_no_meio_status_concluido_com_erros(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")
    conteudo = (
        b"data,valor,descricao\n"
        b"2026-09-05,1500.00,Deposito ok\n"
        b"2026-09-06,NAO_E_NUMERO,Linha quebrada\n"
        b"2026-09-07,300.00,Outro deposito ok\n"
    )

    normalizar_extrato(extrato.id, "csv", conteudo)

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "concluido_com_erros"
    assert extrato_atualizado.quantidade_lancamentos == 2

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 2

    linhas_invalidas = db_session.query(LinhaInvalida).filter_by(extrato_id=extrato.id).all()
    assert len(linhas_invalidas) == 1
    assert linhas_invalidas[0].identificador == "3"
    assert "NAO_E_NUMERO" in linhas_invalidas[0].motivo


def test_normalizar_extrato_csv_todas_linhas_invalidas_status_erro_mas_persiste_linhas_invalidas(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")
    conteudo = (
        b"data,valor,descricao\n"
        b"NAO_E_DATA,100.00,Linha ruim 1\n"
        b"2026-09-06,NAO_E_NUMERO,Linha ruim 2\n"
    )

    normalizar_extrato(extrato.id, "csv", conteudo)

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    # Arquivo estruturalmente ok (tem as 3 colunas certas), mas nenhuma
    # linha aproveitável -> "erro", igual conteúdo corrompido pro usuário —
    # mas, diferente de conteúdo corrompido, o relatório de linhas
    # inválidas é persistido (é justamente o caso onde ele mais importa).
    assert extrato_atualizado.status == "erro"
    assert extrato_atualizado.quantidade_lancamentos == 0

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert lancamentos == []

    linhas_invalidas = db_session.query(LinhaInvalida).filter_by(extrato_id=extrato.id).all()
    assert len(linhas_invalidas) == 2


def test_normalizar_extrato_ofx_transacao_malformada_tolera_e_marca_concluido_com_erros(
    db_session, criar_extrato
):
    # parse_ofx mockado (não dá pra produzir, com um arquivo OFX real, uma
    # transação que passe pela validação estrutural do ofxtools e ainda
    # falhe só na nossa construção do LancamentoNormalizado — ver
    # tests/test_parser_ofx.py, que já prova o comportamento do parser em
    # si). Este teste cobre a ponta que falta: a normalização persistindo
    # o ErroLinha como LinhaInvalida e calculando o status certo.
    resultado_mockado = ResultadoParsing(
        lancamentos=[
            LancamentoNormalizado(
                data=date(2026, 9, 5), valor=Decimal("100.00"), descricao="Ok", tipo="credito"
            )
        ],
        erros=[
            ErroLinha(
                identificador="FIT-BROKEN",
                motivo="'NoneType' object has no attribute 'date'",
            )
        ],
    )

    extrato = criar_extrato("ofx")
    with patch("app.services.normalizacao.parse_ofx", return_value=resultado_mockado):
        normalizar_extrato(extrato.id, "ofx", b"conteudo irrelevante, parse_ofx mockado")

    db_session.expire_all()
    extrato_atualizado = db_session.get(Extrato, extrato.id)
    assert extrato_atualizado.status == "concluido_com_erros"
    assert extrato_atualizado.quantidade_lancamentos == 1

    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 1

    linhas_invalidas = db_session.query(LinhaInvalida).filter_by(extrato_id=extrato.id).all()
    assert len(linhas_invalidas) == 1
    assert linhas_invalidas[0].identificador == "FIT-BROKEN"


# Decisão de desempate de duplicidade (issue #18, ADR-006)


def test_hash_dedup_com_ocorrencia_1_identico_ao_formato_antigo():
    extrato_id = uuid.uuid4()
    esperado = hashlib.sha256(
        f"-150.00|2026-09-05|padaria central|{extrato_id}".encode()
    ).hexdigest()

    assert (
        _calcular_hash_dedup(
            Decimal("-150.00"), date(2026, 9, 5), "  Padaria   CENTRAL ", extrato_id
        )
        == esperado
    )
    assert (
        _calcular_hash_dedup(
            Decimal("-150.00"), date(2026, 9, 5), "  Padaria   CENTRAL ", extrato_id, ocorrencia=1
        )
        == esperado
    )


def test_hash_dedup_com_ocorrencia_maior_que_1_acrescenta_sufixo_e_difere():
    extrato_id = uuid.uuid4()
    hash_2 = _calcular_hash_dedup(
        Decimal("-150.00"), date(2026, 9, 5), "Padaria Central", extrato_id, ocorrencia=2
    )
    esperado = hashlib.sha256(
        f"-150.00|2026-09-05|padaria central|{extrato_id}|2".encode()
    ).hexdigest()

    assert hash_2 == esperado
    assert hash_2 != _calcular_hash_dedup(
        Decimal("-150.00"), date(2026, 9, 5), "Padaria Central", extrato_id
    )


CSV_COM_LINHAS_IDENTICAS = (
    b"data,valor,descricao\n"
    b"2026-09-05,-150.00,Pagamento fornecedor\n"
    b"2026-09-05,-150.00,Pagamento  fornecedor\n"
    b"2026-09-06,300.00,Deposito\n"
)


def test_normalizar_extrato_linhas_identicas_no_arquivo_gravam_dois_lancamentos(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")

    normalizar_extrato(extrato.id, "csv", CSV_COM_LINHAS_IDENTICAS)

    db_session.expire_all()
    lancamentos = (
        db_session.query(Lancamento)
        .filter_by(extrato_id=extrato.id)
        .order_by(Lancamento.valor, Lancamento.ocorrencia)
        .all()
    )
    assert len(lancamentos) == 3
    identicos = [lancamento for lancamento in lancamentos if lancamento.valor == Decimal("-150.00")]
    assert [lancamento.ocorrencia for lancamento in identicos] == [1, 2]
    assert identicos[0].hash_dedup != identicos[1].hash_dedup
    assert [lancamento.ocorrencia for lancamento in lancamentos if lancamento.valor > 0] == [1]

    assert db_session.get(Extrato, extrato.id).quantidade_lancamentos == 3


def test_normalizar_extrato_com_linhas_identicas_reprocessado_nao_duplica_nem_perde(
    db_session, criar_extrato
):
    extrato = criar_extrato("csv")

    normalizar_extrato(extrato.id, "csv", CSV_COM_LINHAS_IDENTICAS)
    normalizar_extrato(extrato.id, "csv", CSV_COM_LINHAS_IDENTICAS)

    db_session.expire_all()
    lancamentos = db_session.query(Lancamento).filter_by(extrato_id=extrato.id).all()
    assert len(lancamentos) == 3
    assert sorted(lancamento.ocorrencia for lancamento in lancamentos) == [1, 1, 2]
    assert db_session.get(Extrato, extrato.id).status == "concluido"
