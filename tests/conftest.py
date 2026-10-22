"""Fixtures compartilhadas de teste.

Testes que gravam no banco (issue #12 em diante — normalização grava
lançamento de verdade) usam a fixture `db_session` abaixo, e as fábricas
`criar_conta`/`criar_empresa`/`criar_usuario`/`criar_extrato` pra montar dado
de teste. Desde a issue #133 (ADR-019), o tenant é a conta: `criar_empresa()`
cria uma conta nova com uma empresa, de id DIFERENTE do da conta (para nenhum
bug que confunda os dois passar escondido), e `criar_empresa(conta_id=...)`
acrescenta uma empresa cliente a uma conta que já existe. A CI roda um
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
import string
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core import bloqueio_login, senha
from app.core import google as modulo_google
from app.core.auth import CABECALHO_EMPRESA, DURACAO_SESSAO_SEGUNDOS
from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.cnpj import digito_verificador
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.rate_limit import limiter
from app.models import (
    ChaveApi,
    CodigoOAuth,
    Conciliacao,
    ConexaoMcp,
    Configuracao,
    Conta,
    Convite,
    DecisaoLinha,
    Empresa,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    PedidoAutorizacaoOAuth,
    TokenOAuth,
    UsoAssistente,
    Usuario,
)
from app.services.consulta_cnpj.cache import cache_por_cnpj as cache_consulta_cnpj
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
def _email_e_ia_de_verdade_desligados(monkeypatch):
    """Nenhum teste fala com o Resend nem com a OpenAI de verdade.

    O `.env` de quem desenvolve pode ter `EMAIL_HABILITADO=true` ou
    `LLM_HABILITADO=true` com uma chave real. O `/register` manda e-mail de
    boas-vindas desde a issue #78, e o `POST /explicacoes` chama o provedor de
    IA: sem isto, um teste que passasse por eles sairia como chamada de
    verdade, paga no caso da IA (issue #87). Quem precisa de envio ou de
    explicação troca a dependency por um provedor falso (fixture `provedor`,
    `app.dependency_overrides`), que não passa por esta configuração."""
    monkeypatch.setattr(settings, "email_habilitado", False)
    monkeypatch.setattr(settings, "llm_habilitado", False)
    # Geração automática ao conciliar (issue #140): desligada, como em produção.
    monkeypatch.setattr(settings, "explicacao_automatica_habilitada", False)
    # Assistente (issue #137): desligado e sem URL de conteúdo, como em
    # produção; quem testa liga e troca a fonte do conteúdo por uma falsa.
    monkeypatch.setattr(settings, "assistente_habilitado", False)
    monkeypatch.setattr(settings, "ajuda_conteudo_url", "")
    # Bloqueio da conciliação pelo plano (issue #122): desligado, como em
    # produção até o gateway; quem testa o bloqueio liga no próprio teste.
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", False)
    # Aprovação de justificativas (issue #132): desligada, como em produção
    # até o front aprovar; quem testa a aprovação liga no próprio teste.
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    # Conexão MCP (issue #136): desligada e sem URLs, como em produção; quem
    # testa liga e configura no próprio teste.
    monkeypatch.setattr(settings, "mcp_habilitado", False)
    monkeypatch.setattr(settings, "mcp_url_publica", "")
    monkeypatch.setattr(settings, "oauth_issuer", "")
    monkeypatch.setattr(settings, "oauth_redirects_permitidos", "")


@pytest.fixture(autouse=True)
def _consulta_cnpj_de_verdade_desligada(monkeypatch):
    """Nenhum teste consulta a Receita de verdade (issue #119): a consulta fica
    desligada, e o cache dela, vazio entre testes. Quem testa a consulta troca
    a dependency por um provedor com `httpx.MockTransport` (fixture `receita`
    de tests/test_consulta_cnpj_cadastro.py)."""
    monkeypatch.setattr(settings, "cnpj_consulta_habilitada", False)
    cache_consulta_cnpj.limpar()
    yield
    cache_consulta_cnpj.limpar()


class _ClienteDeChavesProibido:
    """No lugar do `PyJWKClient` de app/core/google.py: nenhum teste busca as
    chaves do Google de verdade."""

    def get_signing_key_from_jwt(self, token):
        raise AssertionError("teste tentou buscar as chaves do Google de verdade")


@pytest.fixture(autouse=True)
def _google_de_verdade_desligado(monkeypatch):
    """Login pelo Google desligado (`GOOGLE_CLIENT_ID` vazio) e o cliente de
    chaves trocado por um que falha (issue #73). Quem testa o Google usa a
    fixture `google`, com chave RSA gerada na hora."""
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(modulo_google, "_cliente_de_chaves", _ClienteDeChavesProibido())


@pytest.fixture(autouse=True)
def _bloqueio_de_login_zerado():
    """O bloqueio de login por conta (issue #67) é em memória e vale para o
    processo inteiro: sem zerar, as falhas de um teste bloqueariam o e-mail de
    outro."""
    bloqueio_login.limpar_tudo()
    yield
    bloqueio_login.limpar_tudo()


@pytest.fixture(autouse=True)
def _clientes_oauth_do_teste_apagados():
    """Os clientes OAuth (issue #136) não são de nenhuma conta, então a limpeza
    de `criar_conta` não os pega. Autouse, esta limpeza roda depois das outras
    e apaga os criados durante o teste que ficaram sem conexão."""
    inicio = datetime.now(UTC) - timedelta(seconds=1)
    yield
    from app.core.database import engine

    with engine.begin() as conexao:
        conexao.execute(
            text(
                "DELETE FROM clientes_oauth c WHERE c.criado_em >= :inicio AND NOT EXISTS "
                "(SELECT 1 FROM conexoes_mcp m WHERE m.client_id = c.client_id)"
            ),
            {"inicio": inicio},
        )


@pytest.fixture(autouse=True)
def _logs_de_app_chegam_ao_caplog(monkeypatch):
    """O `main` desliga a propagação do logger `app` para a linha não sair
    duplicada (app/core/logging.py, issue #102). O `caplog` lê do logger raiz,
    então nos testes a propagação volta a valer."""
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)


# Valor padrão de `auth_time` em `gerar_token`: o login acabou de acontecer.
LOGIN_AGORA = object()


@pytest.fixture
def gerar_token():
    """Simula o JWT que o NextAuth emite (contrato em app/core/auth.py).

    Desde a issue #75, toda rota autenticada carrega o usuário do `sub`: para
    passar, o token precisa do id de um usuário real da conta (ou use
    `auth_headers`, que cria um). `auth_time` é o login, em segundos
    inteiros; `None` tira o claim do token.

    O primeiro argumento é o id da CONTA (issue #133), no claim `conta_id`
    (vetor v3 do ADR-005). `claim="empresa_id"` emite o token legado, de antes
    da #133, com o mesmo valor no claim `empresa_id`."""

    def _gerar(
        conta_id: uuid.UUID | str | None,
        secret: str = SECRET_TESTE,
        expira_em: timedelta = timedelta(seconds=DURACAO_SESSAO_SEGUNDOS),
        usuario_id: uuid.UUID | str | None = None,
        auth_time: object = LOGIN_AGORA,
        claim: str = "conta_id",
    ) -> str:
        agora = datetime.now(UTC)
        claims = {
            "sub": str(usuario_id or uuid.uuid4()),
            "email": "usuario@teste.com",
            "iat": agora,
            "exp": agora + expira_em,
        }
        if auth_time is LOGIN_AGORA:
            claims["auth_time"] = int(agora.timestamp())
        elif auth_time is not None:
            claims["auth_time"] = auth_time
        if conta_id is not None:
            claims[claim] = str(conta_id)
        return jwt.encode(claims, secret, algorithm="HS256")

    return _gerar


@pytest.fixture
def auth_headers(gerar_token, request):
    """Cabeçalhos de um usuário real da conta da empresa (issues #75 e #133):
    o token, com o `conta_id`, e `X-Ledgr-Empresa` com a empresa, que vira a
    empresa ativa. Na primeira chamada para cada conta, cria um usuário nela,
    sem senha e com papel 'gestor', que `criar_usuario` apaga no fim. A
    empresa precisa existir no banco. `ativa=False` tira o cabeçalho da
    empresa (o front de antes da #133)."""
    usuarios: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}

    def _headers(empresa_id: uuid.UUID | str, ativa: bool = True) -> dict[str, str]:
        empresa_id = uuid.UUID(str(empresa_id))
        if empresa_id not in usuarios:
            db_session = request.getfixturevalue("db_session")
            conta_id = db_session.get(Empresa, empresa_id).conta_id
            criar_usuario = request.getfixturevalue("criar_usuario")
            usuario_id = (
                next(
                    (u for u, c in usuarios.values() if c == conta_id),
                    None,
                )
                or criar_usuario(com_senha=False, conta_id=conta_id).id
            )
            usuarios[empresa_id] = (usuario_id, conta_id)
        usuario_id, conta_id = usuarios[empresa_id]
        token = gerar_token(conta_id, usuario_id=usuario_id)
        headers = {"Authorization": f"Bearer {token}"}
        if ativa:
            headers[CABECALHO_EMPRESA] = str(empresa_id)
        return headers

    return _headers


GOOGLE_CLIENT_ID_TESTE = "ledgr-teste.apps.googleusercontent.com"
KID_GOOGLE_TESTE = "kid-de-teste"


@pytest.fixture(scope="session")
def _chaves_rsa():
    """Duas chaves RSA, geradas uma vez por sessão: a do "Google" e outra,
    para a assinatura errada."""
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in "12")


class ClienteDeChavesFalso:
    """Faz o papel do `PyJWKClient`: devolve a chave pública do `kid` do
    token, sem rede. Com `falha_de_conexao`, falha como o Google fora do ar.
    `buscas` conta as chamadas; `conexoes_em_uso` guarda quantas conexões do
    pool estavam em uso em cada uma."""

    def __init__(self, chave_publica) -> None:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(chave_publica, as_dict=True)
        self._chaves = {
            KID_GOOGLE_TESTE: jwt.PyJWK({**jwk, "kid": KID_GOOGLE_TESTE, "alg": "RS256"})
        }
        self.falha_de_conexao = False
        self.buscas = 0
        self.conexoes_em_uso: list[int] = []

    def get_signing_key_from_jwt(self, token):
        self.buscas += 1
        self.conexoes_em_uso.append(engine.pool.checkedout())
        if self.falha_de_conexao:
            raise jwt.PyJWKClientConnectionError("Google fora do ar (teste)")
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._chaves:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
        return self._chaves[kid]


class GoogleFalso:
    """Emite ID tokens como o Google, assinados com a chave de teste."""

    def __init__(self, chave_privada, outra_chave, cliente: ClienteDeChavesFalso) -> None:
        self._chave_privada = chave_privada
        self._outra_chave = outra_chave
        self.cliente = cliente

    def id_token(
        self,
        email: str | None = "maria@gmail.com",
        nome: str | None = "Maria Google",
        assinar_com_outra_chave: bool = False,
        kid: str = KID_GOOGLE_TESTE,
        **claims,
    ) -> str:
        """`claims` sobrescreve qualquer claim; valor `None` tira o claim."""
        agora = datetime.now(UTC)
        payload = {
            "iss": "https://accounts.google.com",
            "azp": GOOGLE_CLIENT_ID_TESTE,
            "aud": GOOGLE_CLIENT_ID_TESTE,
            "sub": f"1{random.randint(10**19, 10**20 - 1)}",
            "email": email,
            "email_verified": True,
            "name": nome,
            "iat": agora,
            "exp": agora + timedelta(hours=1),
            **claims,
        }
        payload = {chave: valor for chave, valor in payload.items() if valor is not None}
        chave = self._outra_chave if assinar_com_outra_chave else self._chave_privada
        return jwt.encode(payload, chave, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def google(monkeypatch, _chaves_rsa):
    """Login pelo Google ligado com `GOOGLE_CLIENT_ID_TESTE` e o cliente de
    chaves falso. Nada sai para a rede."""
    chave_privada, outra_chave = _chaves_rsa
    cliente = ClienteDeChavesFalso(chave_privada.public_key())
    monkeypatch.setattr(settings, "google_client_id", GOOGLE_CLIENT_ID_TESTE)
    monkeypatch.setattr(modulo_google, "_cliente_de_chaves", cliente)
    return GoogleFalso(chave_privada, outra_chave, cliente)


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


def _gerar_cnpj_alfanumerico() -> str:
    """CNPJ alfanumérico fictício com dígitos verificadores válidos e sempre
    com letra na raiz: a primeira posição é sorteada só entre as letras. A raiz
    vinha de uuid4().hex[:8], que só tem 0-9 e A-F; em (10/16)^8 ≈ 2,3% das
    vezes saía só dígito e o CNPJ "alfanumérico" era numérico."""
    raiz = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=7)
    )
    base = f"{raiz}0001"
    base += digito_verificador(base)
    return base + digito_verificador(base)


@pytest.fixture
def gerar_cnpj():
    return _gerar_cnpj


def _apagar_conexoes_mcp(db_session, *condicoes) -> None:
    """Conexões MCP (issue #136) e o que aponta para elas, sem commit."""
    ids = select(ConexaoMcp.id).where(*condicoes)
    db_session.query(TokenOAuth).filter(TokenOAuth.conexao_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.query(CodigoOAuth).filter(CodigoOAuth.conexao_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.query(ConexaoMcp).filter(*condicoes).delete(synchronize_session=False)


def _apagar_empresa(db_session, empresa_id: uuid.UUID) -> None:
    """Apaga a empresa e tudo o que é dela, sem commit. Convites e usuários são
    da conta desde a issue #133 e saem na limpeza de `criar_conta`."""
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
        db_session.query(LinhaInvalida).filter(LinhaInvalida.extrato_id.in_(extrato_ids)).delete(
            synchronize_session=False
        )
        db_session.query(Extrato).filter(Extrato.id.in_(extrato_ids)).delete(
            synchronize_session=False
        )
    # Configuracao e ExplicacaoDivergencia são FK direta de Empresa (não
    # passam por Extrato) — apagar antes da Empresa, senão a FK barra o
    # delete (issue #22 foi o primeiro teste a gravar Configuracao aqui;
    # issue #28/ADR-011 acrescenta o cache de explicações).
    db_session.query(Configuracao).filter_by(empresa_id=empresa_id).delete()
    db_session.query(ExplicacaoDivergencia).filter_by(empresa_id=empresa_id).delete()
    db_session.query(ChaveApi).filter_by(empresa_id=empresa_id).delete()
    _apagar_conexoes_mcp(db_session, ConexaoMcp.empresa_id == empresa_id)
    db_session.query(Empresa).filter_by(id=empresa_id).delete()


@pytest.fixture
def criar_conta(db_session):
    """Fábrica de Conta real no banco (issue #133), sem empresa. A limpeza no
    fim do teste apaga a conta, os convites e os usuários dela, inclusive os
    criados via HTTP (aceite de convite), e qualquer empresa que tenha sobrado
    nela, com os dados (as criadas pelo `POST /empresas`, por exemplo). Roda
    depois da limpeza de `criar_empresa`, que depende desta."""
    contas_criadas: list[uuid.UUID] = []

    def _criar(razao_social: str = "Conta de teste", cnpj: str | None = None) -> Conta:
        conta = Conta(razao_social=razao_social, cnpj=cnpj or _gerar_cnpj())
        db_session.add(conta)
        db_session.commit()
        contas_criadas.append(conta.id)
        return conta

    yield _criar

    db_session.rollback()
    for conta_id in contas_criadas:
        # Empresas criadas pela API (POST /empresas), fora de `criar_empresa`.
        for empresa_id in list(
            db_session.scalars(select(Empresa.id).where(Empresa.conta_id == conta_id))
        ):
            _apagar_empresa(db_session, empresa_id)
        db_session.query(DecisaoLinha).filter(
            DecisaoLinha.usuario_id.in_(select(Usuario.id).where(Usuario.conta_id == conta_id))
        ).delete(synchronize_session=False)
        db_session.query(Convite).filter_by(conta_id=conta_id).delete()
        db_session.query(ChaveApi).filter_by(conta_id=conta_id).delete()
        _apagar_conexoes_mcp(db_session, ConexaoMcp.conta_id == conta_id)
        db_session.query(PedidoAutorizacaoOAuth).filter_by(conta_id=conta_id).delete()
        db_session.query(Usuario).filter_by(conta_id=conta_id).delete()
        db_session.query(UsoAssistente).filter_by(conta_id=conta_id).delete()
        db_session.query(Conta).filter_by(id=conta_id).delete()
    db_session.commit()


@pytest.fixture
def criar_empresa(db_session, criar_conta):
    """Fábrica de Empresa real no banco. Sem `conta_id`, cria uma conta nova
    com a mesma razão social e o mesmo CNPJ, como o `/register`, e a empresa
    com id próprio, diferente do da conta. Com `conta_id`, acrescenta uma
    empresa cliente à conta.

    A limpeza no fim do teste apaga só o que essa fábrica criou: a empresa e
    qualquer extrato/lançamento/linha inválida/configuração/execução de
    conciliação gravado contra ela durante o teste — inclusive via HTTP, não
    só pelos outros helpers deste arquivo. A conta sai na limpeza de
    `criar_conta`."""
    empresas_criadas: list[uuid.UUID] = []

    def _criar(
        razao_social: str = "Empresa de teste",
        conta_id: uuid.UUID | None = None,
        cnpj: str | None = None,
    ) -> Empresa:
        cnpj = cnpj or _gerar_cnpj()
        if conta_id is None:
            conta_id = criar_conta(razao_social, cnpj).id
        empresa = Empresa(conta_id=conta_id, razao_social=razao_social, cnpj=cnpj)
        db_session.add(empresa)
        db_session.commit()
        empresas_criadas.append(empresa.id)
        return empresa

    yield _criar

    for empresa_id in empresas_criadas:
        _apagar_empresa(db_session, empresa_id)
    db_session.commit()


def empresa_unica(db_session, conta_id: uuid.UUID) -> uuid.UUID:
    """O id da única empresa da conta: a que `criar_usuario()` e o `/register`
    criam junto com a conta (issue #133)."""
    (empresa_id,) = db_session.scalars(select(Empresa.id).where(Empresa.conta_id == conta_id))
    return empresa_id


def cabecalhos(token: str, empresa_id: uuid.UUID | None = None) -> dict[str, str]:
    """`Authorization` com o token e, com `empresa_id`, a empresa ativa."""
    headers = {"Authorization": f"Bearer {token}"}
    if empresa_id is not None:
        headers[CABECALHO_EMPRESA] = str(empresa_id)
    return headers


def apagar_contas_por_cnpj(db_session, cnpjs: list[str]) -> None:
    """Apaga o que o cadastro pela API cria (issue #133): a conta, a primeira
    empresa, a configuração dela e os usuários da conta, achados pelo CNPJ da
    conta. Para os testes do `/register` e do `/register/google`, que não
    passam pelas fábricas. Sem commit."""
    conta_ids = list(db_session.scalars(select(Conta.id).where(Conta.cnpj.in_(cnpjs))))
    empresa_ids = list(
        db_session.scalars(select(Empresa.id).where(Empresa.conta_id.in_(conta_ids)))
    )
    db_session.query(Usuario).filter(Usuario.conta_id.in_(conta_ids)).delete(
        synchronize_session=False
    )
    db_session.query(Configuracao).filter(Configuracao.empresa_id.in_(empresa_ids)).delete(
        synchronize_session=False
    )
    db_session.query(Empresa).filter(Empresa.id.in_(empresa_ids)).delete(synchronize_session=False)
    db_session.query(Conta).filter(Conta.id.in_(conta_ids)).delete(synchronize_session=False)


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
    """Usuário real no banco, com a senha `SENHA_USUARIO_TESTE` (ou sem senha,
    como conta do Google). Sem `empresa_id` nem `conta_id`, numa conta nova
    (com uma empresa) e com papel 'admin', como no `/register`; com um dos
    dois, entra na conta (a da empresa, com `empresa_id`) com papel 'gestor',
    o que os convidados de antes da issue #133 viraram, ou com o `papel`
    dado. Apaga só os que criou, antes da limpeza de `criar_empresa` (os
    tokens de `tokens_email` caem junto, pelo ON DELETE CASCADE)."""
    criados: list[uuid.UUID] = []

    def _criar(
        com_senha: bool = True,
        empresa_id: uuid.UUID | None = None,
        nome: str = "Maria Financeiro",
        conta_id: uuid.UUID | None = None,
        papel: str | None = None,
    ) -> Usuario:
        if empresa_id is not None:
            conta_id = db_session.get(Empresa, empresa_id).conta_id
        if conta_id is None:
            conta_id = criar_empresa().conta_id
            papel = papel or "admin"
        usuario = Usuario(
            conta_id=conta_id,
            nome=nome,
            email=f"{uuid.uuid4().hex[:12]}@teste.com",
            senha_hash=senha.gerar_hash(SENHA_USUARIO_TESTE) if com_senha else None,
            google_sub=None if com_senha else f"google-{uuid.uuid4().hex}",
            papel=papel or "gestor",
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


# A última revisão antes das contas (issue #133). Os testes das migrações
# anteriores inserem empresa e usuário no esquema de antes dela (empresa sem
# `conta_id`, usuário com `empresa_id` e `administrador`): sobem até aqui, que
# é o esquema que eles testam, e não até o head.
REVISAO_ANTES_DAS_CONTAS = "d5a8f1c3e690"


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
