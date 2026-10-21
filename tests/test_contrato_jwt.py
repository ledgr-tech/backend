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

A versão 3 (issue #133, ADR-019) troca o claim `empresa_id` por `conta_id`,
com o mesmo valor: gerada do mesmo jeito, por uma cópia de leitura de
lib/token.ts com a troca (registrado em `gerado_com`). O mesmo procedimento,
sem a troca, reproduz o token da v2. A v2 fica em
`tests/fixtures/contrato_jwt_v2.json`, para o caso do claim legado: o backend
continua aceitando o `empresa_id` como id da conta até o front migrar.

Desde a #75 a dependency carrega o usuário do `sub`, então todo teste que
passa pela dependency usa a conta com os ids do vetor (`conta_do_vetor`).
Desde a #133 o id do vetor é o da conta, e a empresa da conta tem id próprio.

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

from app.core.auth import DURACAO_SESSAO_SEGUNDOS, EmpresaAtiva, obter_empresa_ativa
from app.core.config import settings
from app.models import Conta, Empresa, Usuario
from main import app

FIXTURES = Path(__file__).parent / "fixtures"
VETOR = json.loads((FIXTURES / "contrato_jwt.json").read_text(encoding="utf-8"))
# A versão 2, com o claim legado `empresa_id` (issue #133).
VETOR_V2 = json.loads((FIXTURES / "contrato_jwt_v2.json").read_text(encoding="utf-8"))
EMPRESA_DA_CONTA = uuid.UUID("33333333-3333-4333-8333-333333333333")
DENTRO_DA_VALIDADE = "2026-10-07T13:00:00Z"
DEPOIS_DO_EXP = "2026-10-15T12:00:00Z"

# App mínimo, só deste teste: a MESMA dependency que as rotas de empresa usam.
app_minimo = FastAPI()


@app_minimo.get("/empresa-do-token")
def _empresa_do_token(
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
) -> dict[str, str]:
    return {"conta_id": str(ativa.usuario.conta_id), "empresa_id": str(ativa.empresa.id)}


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


@pytest.mark.parametrize("vetor", [VETOR, VETOR_V2], ids=["v3", "v2"])
def test_vetor_tem_os_campos_esperados_e_segredo_proprio(vetor):
    assert set(vetor) == {
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
    assert vetor["segredo"] != SECRET_TESTE
    assert vetor["token"].count(".") == 2


def test_v3_troca_o_claim_e_mantem_o_resto_da_v2():
    assert (VETOR["versao"], VETOR_V2["versao"]) == (3, 2)
    assert set(VETOR["claims_entrada"]) == {"usuarioId", "contaId", "email", "authTime"}
    assert set(VETOR_V2["claims_entrada"]) == {"usuarioId", "empresaId", "email", "authTime"}
    assert VETOR["claims_entrada"]["contaId"] == VETOR_V2["claims_entrada"]["empresaId"]
    assert set(VETOR["payload"]) == {"conta_id", "email", "auth_time", "sub", "iat", "exp"}
    assert set(VETOR_V2["payload"]) == {"empresa_id", "email", "auth_time", "sub", "iat", "exp"}
    assert VETOR["payload"]["conta_id"] == VETOR_V2["payload"]["empresa_id"]
    sem_tenant = {k: v for k, v in VETOR["payload"].items() if k != "conta_id"}
    assert sem_tenant == {k: v for k, v in VETOR_V2["payload"].items() if k != "empresa_id"}
    assert (VETOR["segredo"], VETOR["instante_utc"]) == (
        VETOR_V2["segredo"],
        VETOR_V2["instante_utc"],
    )


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
    assert payload["conta_id"] == VETOR["claims_entrada"]["contaId"]
    assert payload["email"] == VETOR["claims_entrada"]["email"]
    assert payload["auth_time"] == VETOR["claims_entrada"]["authTime"]
    # o vetor é de um login: a sessão começa quando o token é assinado
    assert payload["auth_time"] == payload["iat"]
    assert payload["exp"] - payload["iat"] == DURACAO_SESSAO_SEGUNDOS


# A conta com os ids do vetor (Postgres, roda na CI)


def _apagar_conta_do_vetor(db_session) -> None:
    db_session.rollback()
    db_session.query(Usuario).filter_by(id=uuid.UUID(VETOR["payload"]["sub"])).delete()
    db_session.query(Empresa).filter_by(id=EMPRESA_DA_CONTA).delete()
    db_session.query(Conta).filter_by(id=uuid.UUID(VETOR["payload"]["conta_id"])).delete()
    db_session.commit()


@pytest.fixture
def conta_do_vetor(db_session, gerar_cnpj):
    """Conta, empresa e usuário com os ids FIXOS do vetor (a empresa com id
    próprio, diferente do da conta). Como os ids são fixos, apaga uma sobra de
    execução interrompida antes de criar, e apaga de novo no fim; não toca em
    nenhuma outra linha."""
    _apagar_conta_do_vetor(db_session)
    cnpj = gerar_cnpj()
    conta = Conta(
        id=uuid.UUID(VETOR["payload"]["conta_id"]), razao_social="Conta do contrato", cnpj=cnpj
    )
    empresa = Empresa(
        id=EMPRESA_DA_CONTA, conta_id=conta.id, razao_social="Conta do contrato", cnpj=cnpj
    )
    usuario = Usuario(
        id=uuid.UUID(VETOR["payload"]["sub"]),
        conta_id=conta.id,
        nome="Contrato JWT",
        email=VETOR["payload"]["email"],
        senha_hash="hash-qualquer",
        papel="admin",
    )
    db_session.add(conta)
    db_session.commit()
    db_session.add_all([empresa, usuario])
    db_session.commit()
    try:
        yield usuario
    finally:
        _apagar_conta_do_vetor(db_session)


# A dependency das rotas aceita o token do front


ESPERADO = {"conta_id": VETOR["payload"]["conta_id"], "empresa_id": str(EMPRESA_DA_CONTA)}


@freeze_time(DENTRO_DA_VALIDADE)
def test_dependency_aceita_o_token_do_front(conta_do_vetor, segredo_do_vetor):
    resposta = _empresa_do_token(VETOR["token"])

    assert resposta.status_code == 200
    assert resposta.json() == ESPERADO


@freeze_time(DENTRO_DA_VALIDADE)
def test_dependency_aceita_o_token_da_v2_com_o_claim_legado(conta_do_vetor, segredo_do_vetor):
    """O token do front de antes da #133: `empresa_id` é lido como o id da
    conta, e a conta de uma empresa só dispensa `X-Ledgr-Empresa`."""
    resposta = _empresa_do_token(VETOR_V2["token"])

    assert resposta.status_code == 200
    assert resposta.json() == ESPERADO


@freeze_time(DENTRO_DA_VALIDADE)
def test_conta_id_e_empresa_id_divergentes_da_401(conta_do_vetor, segredo_do_vetor):
    claims = {**VETOR["payload"], "empresa_id": str(EMPRESA_DA_CONTA)}
    token = jwt.encode(claims, VETOR["segredo"], algorithm="HS256")

    assert _empresa_do_token(token).status_code == 401


@freeze_time(DENTRO_DA_VALIDADE)
def test_conta_id_e_empresa_id_iguais_passam(conta_do_vetor, segredo_do_vetor):
    claims = {**VETOR["payload"], "empresa_id": VETOR["payload"]["conta_id"]}
    token = jwt.encode(claims, VETOR["segredo"], algorithm="HS256")

    assert _empresa_do_token(token).json() == ESPERADO


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
def test_token_sem_conta_id_nem_empresa_id_da_401(conta_do_vetor, segredo_do_vetor):
    claims = {k: v for k, v in VETOR["payload"].items() if k != "conta_id"}
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
    assert corpo["conta_id"] == corpo["empresa_id"] == VETOR["payload"]["conta_id"]
    assert corpo["email"] == VETOR["payload"]["email"]
    assert [empresa["id"] for empresa in corpo["empresas"]] == [str(EMPRESA_DA_CONTA)]
