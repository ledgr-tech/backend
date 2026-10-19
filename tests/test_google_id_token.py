"""Conferência do ID token do Google (issue #73), em app/core/google.py.

Nenhum teste chama o Google: a fixture `google` (tests/conftest.py) gera uma
chave RSA na hora e troca o cliente de chaves por um falso.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import GOOGLE_CLIENT_ID_TESTE

from app.core import google as modulo_google
from app.core.config import settings
from app.core.google import (
    URL_CHAVES_GOOGLE,
    ContaGoogleInvalida,
    GoogleIndisponivel,
    IdentidadeGoogle,
    conferir_id_token,
)


def _agora() -> datetime:
    return datetime.now(UTC)


def test_token_valido_devolve_sub_email_normalizado_e_nome(google):
    token = google.id_token(email="  Maria.Silva@GMAIL.com ", sub="1234567890", nome=" Maria ")

    assert conferir_id_token(token) == IdentidadeGoogle(
        sub="1234567890", email="maria.silva@gmail.com", nome="Maria"
    )


@pytest.mark.parametrize("emissor", ["accounts.google.com", "https://accounts.google.com"])
def test_aceita_os_dois_emissores_do_google(google, emissor):
    assert conferir_id_token(google.id_token(iss=emissor)).email == "maria@gmail.com"


@pytest.mark.parametrize("nome", [None, "", "   "])
def test_sem_nome_no_token_devolve_nome_vazio(google, nome):
    assert conferir_id_token(google.id_token(nome=nome)).nome is None


# Calculados na hora do teste, não na coleta.
TOKEN_RECUSADO = {
    "vencido": lambda g: g.id_token(
        iat=_agora() - timedelta(hours=2), exp=_agora() - timedelta(minutes=1)
    ),
    "sem_exp": lambda g: g.id_token(exp=None),
    "de_outra_audiencia": lambda g: g.id_token(aud="outro-app.apps.googleusercontent.com"),
    "sem_audiencia": lambda g: g.id_token(aud=None),
    "de_outro_emissor": lambda g: g.id_token(iss="https://accounts.google.com.evil.example"),
    "sem_emissor": lambda g: g.id_token(iss=None),
    "email_nao_verificado": lambda g: g.id_token(email_verified=False),
    "email_verified_ausente": lambda g: g.id_token(email_verified=None),
    "email_verified_texto": lambda g: g.id_token(email_verified="true"),
    "sem_email": lambda g: g.id_token(email=None),
    "email_vazio": lambda g: g.id_token(email="  "),
    "sem_sub": lambda g: g.id_token(sub=None),
    "sub_numerico": lambda g: g.id_token(sub=123),
    "assinatura_errada": lambda g: g.id_token(assinar_com_outra_chave=True),
    "kid_desconhecido": lambda g: g.id_token(kid="kid-que-o-google-nao-tem"),
    "emitido_no_futuro": lambda g: g.id_token(
        iat=_agora() + timedelta(minutes=5), exp=_agora() + timedelta(hours=1)
    ),
    "nao_e_jwt": lambda g: "isto-nao-e-um-jwt",
}


@pytest.mark.parametrize("caso", TOKEN_RECUSADO)
def test_token_invalido_levanta_conta_google_invalida(google, caso):
    token = TOKEN_RECUSADO[caso](google)

    with pytest.raises(ContaGoogleInvalida):
        conferir_id_token(token)


def test_token_hs256_com_o_client_id_como_segredo_e_recusado(google):
    """Confusão de algoritmo: só RS256 vale."""
    token = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": GOOGLE_CLIENT_ID_TESTE,
            "sub": "1",
            "email": "maria@gmail.com",
            "email_verified": True,
            "iat": _agora(),
            "exp": _agora() + timedelta(hours=1),
        },
        GOOGLE_CLIENT_ID_TESTE,
        algorithm="HS256",
        headers={"kid": "kid-de-teste"},
    )

    with pytest.raises(ContaGoogleInvalida):
        conferir_id_token(token)


def test_folga_de_relogio_aceita_iat_poucos_segundos_no_futuro(google):
    """O `iat` vem do relógio do Google; o do Railway pode estar um pouco atrás."""
    token = google.id_token(iat=_agora() + timedelta(seconds=5))

    assert conferir_id_token(token).email == "maria@gmail.com"


def test_client_id_vazio_e_indisponivel_sem_buscar_as_chaves(google, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "  ")

    with pytest.raises(GoogleIndisponivel):
        conferir_id_token(google.id_token())
    assert google.cliente.buscas == 0


def test_falha_ao_buscar_as_chaves_e_indisponivel(google):
    google.cliente.falha_de_conexao = True

    with pytest.raises(GoogleIndisponivel):
        conferir_id_token(google.id_token())


def test_cliente_de_chaves_do_google_com_cache_e_timeout_curto():
    """O cliente de verdade, sem buscar nada: só a configuração."""
    cliente = modulo_google._criar_cliente_de_chaves()

    assert cliente.uri == URL_CHAVES_GOOGLE == "https://www.googleapis.com/oauth2/v3/certs"
    assert cliente.timeout <= 5
    assert cliente.jwk_set_cache is not None
    assert cliente.jwk_set_cache.lifespan >= 300
