"""Teste de contrato do JWT entre o frontend (NextAuth) e o backend (issue #71,
ADR-003 e ADR-005).

Até aqui cada lado testava só a sua metade (a fixture `gerar_token` aqui,
`lib/token.test.ts` no front), e o contrato já quebrou uma vez sem teste
perceber (cookie criptografado do NextAuth). Estes testes usam um VETOR fixo,
`tests/fixtures/contrato_jwt.json`, com o relógio parado em `instante_utc` e
um segredo só de teste (nunca usar em .env nem em produção). A mesma cópia fica
no vault, em `02-decisoes/anexos/05-contrato-jwt-vetor.json`, e o teste do front
confere que `assinarToken` produz exatamente esse token.

A versão 1 veio do `assinarToken` REAL do front (lib/token.ts, develop
a50915c). A versão 2 (issue #75) acrescenta o claim `auth_time`, igual ao
`iat`: foi gerada por uma cópia de leitura de lib/token.ts com `auth_time`
acrescentado como descrito na nota de integração, e não pelo código do front,
que ainda não mudou. O teste do front confirma quando a mudança entrar lá.

Desde a #75 a dependency carrega o usuário do `sub`, então todo teste que
passa pela dependency usa a conta com os ids do vetor (`conta_do_vetor`).

O relógio é congelado com freezegun (que congela `datetime.now` e `time.time`,
os dois que o PyJWT usa, e o `datetime.now` que confere o `auth_time`), porque o
token vence em 7 dias (exp em 14/10/2026):
sem isso, o teste passaria a falhar sozinho depois dessa data.

Qualquer mudança de formato do token (claims, algoritmo, validade) obriga a
atualizar juntos o vetor (nos dois lugares), o ADR-005, este teste e o teste
do front.
"""

import json
import uuid
from pathlib import Path
from typing import Annotated

import jwt
import pytest
from conftest import SECRET_TESTE
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.core.auth import DURACAO_SESSAO_SEGUNDOS, obter_empresa_id_autenticada
from app.core.config import settings
from app.models import Empresa, Usuario
from main import app

VETOR = json.loads(
    (Path(__file__).parent / "fixtures" / "contrato_jwt.json").read_text(encoding="utf-8")
)
DENTRO_DA_VALIDADE = "2026-10-07T13:00:00Z"
DEPOIS_DO_EXP = "2026-10-15T12:00:00Z"

# App mínimo, só deste teste: a MESMA dependency que as rotas de empresa usam.
app_minimo = FastAPI()


@app_minimo.get("/empresa-do-token")
def _empresa_do_token(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
) -> dict[str, str]:
    return {"empresa_id": str(empresa_id)}


cliente_minimo = TestClient(app_minimo)
cliente = TestClient(app)


@pytest.fixture
def segredo_do_vetor(monkeypatch):
    """Depois da fixture autouse do conftest, que fixa o segredo de teste."""
    monkeypatch.setattr(settings, "nextauth_secret", VETOR["segredo"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _empresa_do_token(token: str):
    return cliente_minimo.get("/empresa-do-token", headers=_bearer(token))


# O arquivo do vetor


def test_vetor_tem_os_campos_esperados_e_segredo_proprio():
    assert set(VETOR) == {
        "versao",
        "descricao",
        "gerado_com",
        "segredo",
        "instante_utc",
        "claims_entrada",
        "token",
        "header",
        "payload",
    }
    assert VETOR["versao"] == 2
    assert set(VETOR["claims_entrada"]) == {"usuarioId", "empresaId", "email", "authTime"}
    assert VETOR["segredo"] != SECRET_TESTE
    assert VETOR["token"].count(".") == 2


# Formato do token


def test_header_e_payload_batem_com_o_vetor():
    assert jwt.get_unverified_header(VETOR["token"]) == VETOR["header"] == {"alg": "HS256"}
    payload = jwt.decode(
        VETOR["token"],
        VETOR["segredo"],
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert payload == VETOR["payload"]
    assert payload["sub"] == VETOR["claims_entrada"]["usuarioId"]
    assert payload["empresa_id"] == VETOR["claims_entrada"]["empresaId"]
    assert payload["email"] == VETOR["claims_entrada"]["email"]
    assert payload["auth_time"] == VETOR["claims_entrada"]["authTime"]
    # o vetor é de um login: a sessão começa quando o token é assinado
    assert payload["auth_time"] == payload["iat"]
    assert payload["exp"] - payload["iat"] == DURACAO_SESSAO_SEGUNDOS


# A conta com os ids do vetor (Postgres, roda na CI)


def _apagar_conta_do_vetor(db_session) -> None:
    db_session.rollback()
    db_session.query(Usuario).filter_by(id=uuid.UUID(VETOR["payload"]["sub"])).delete()
    db_session.query(Empresa).filter_by(id=uuid.UUID(VETOR["payload"]["empresa_id"])).delete()
    db_session.commit()


@pytest.fixture
def conta_do_vetor(db_session, gerar_cnpj):
    """Empresa e usuário com os ids FIXOS do vetor. Como os ids são fixos,
    apaga uma sobra de execução interrompida antes de criar, e apaga de novo
    no fim; não toca em nenhuma outra linha."""
    _apagar_conta_do_vetor(db_session)
    empresa = Empresa(
        id=uuid.UUID(VETOR["payload"]["empresa_id"]),
        razao_social="Empresa do contrato",
        cnpj=gerar_cnpj(),
    )
    usuario = Usuario(
        id=uuid.UUID(VETOR["payload"]["sub"]),
        empresa_id=empresa.id,
        nome="Contrato JWT",
        email=VETOR["payload"]["email"],
        senha_hash="hash-qualquer",
    )
    db_session.add(empresa)
    db_session.commit()
    db_session.add(usuario)
    db_session.commit()
    try:
        yield usuario
    finally:
        _apagar_conta_do_vetor(db_session)


# A dependency das rotas aceita o token do front


@freeze_time(DENTRO_DA_VALIDADE)
def test_dependency_aceita_o_token_do_front(conta_do_vetor, segredo_do_vetor):
    resposta = _empresa_do_token(VETOR["token"])

    assert resposta.status_code == 200
    assert resposta.json() == {"empresa_id": VETOR["payload"]["empresa_id"]}


@freeze_time(DEPOIS_DO_EXP)
def test_token_vencido_da_401(conta_do_vetor, segredo_do_vetor):
    assert _empresa_do_token(VETOR["token"]).status_code == 401


@freeze_time(DENTRO_DA_VALIDADE)
def test_segredo_diferente_da_401(conta_do_vetor, monkeypatch):
    monkeypatch.setattr(settings, "nextauth_secret", "outro-segredo-qualquer-de-teste")

    assert _empresa_do_token(VETOR["token"]).status_code == 401


@freeze_time(DENTRO_DA_VALIDADE)
def test_payload_adulterado_da_401(conta_do_vetor, segredo_do_vetor):
    cabecalho, payload, assinatura = VETOR["token"].split(".")
    meio = len(payload) // 2
    trocado = "A" if payload[meio] != "A" else "B"
    adulterado = f"{cabecalho}.{payload[:meio]}{trocado}{payload[meio + 1 :]}.{assinatura}"

    assert adulterado != VETOR["token"]
    assert _empresa_do_token(adulterado).status_code == 401


@freeze_time(DENTRO_DA_VALIDADE)
def test_token_sem_empresa_id_da_401(conta_do_vetor, segredo_do_vetor):
    claims = {k: v for k, v in VETOR["payload"].items() if k != "empresa_id"}
    token = jwt.encode(claims, VETOR["segredo"], algorithm="HS256")

    assert _empresa_do_token(token).status_code == 401


@freeze_time(DENTRO_DA_VALIDADE)
def test_token_sem_auth_time_da_401(conta_do_vetor, segredo_do_vetor):
    """O token do front antes da #75: tudo igual, só sem `auth_time`."""
    claims = {k: v for k, v in VETOR["payload"].items() if k != "auth_time"}
    token = jwt.encode(claims, VETOR["segredo"], algorithm="HS256")

    assert _empresa_do_token(token).status_code == 401


# Ponta a ponta com GET /me (Postgres, roda na CI)


@freeze_time(DENTRO_DA_VALIDADE)
def test_get_me_aceita_o_token_do_front(conta_do_vetor, segredo_do_vetor):
    resposta = cliente.get("/me", headers=_bearer(VETOR["token"]))

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["id"] == VETOR["payload"]["sub"]
    assert corpo["empresa_id"] == VETOR["payload"]["empresa_id"]
    assert corpo["email"] == VETOR["payload"]["email"]
