"""Conferência do ID token do Google (issue #73, adendo de 13/10 do ADR-004).

O backend confere a identidade sozinho. O front manda o `id_token` que o
provider do Google entrega ao NextAuth, e o backend valida:

- a assinatura RS256, com as chaves públicas do Google (`URL_CHAVES_GOOGLE`);
- `aud` igual a `GOOGLE_CLIENT_ID`, o client OAuth da Ledgr;
- `iss` igual a `accounts.google.com` ou `https://accounts.google.com`;
- `exp` (exigido) e `iat`, com `FOLGA_RELOGIO_SEGUNDOS`;
- `email_verified` verdadeiro.

Nunca aceita só o `sub` ou o e-mail mandados pelo front: qualquer um chamaria
a rota direto com o e-mail de outra pessoa.

As chaves vêm de um `PyJWKClient` com cache do conjunto de chaves
(`VALIDADE_CACHE_CHAVES_SEGUNDOS`) e timeout curto. Um `kid` desconhecido
força uma busca nova, no máximo uma a cada 30 segundos (o padrão do PyJWT).
A busca é uma chamada HTTP que pode demorar até o timeout: as rotas conferem
o token antes de tocar no banco, para nunca segurar uma transação aberta
esperando o Google.

`GOOGLE_CLIENT_ID` vazio deixa o login pelo Google indisponível, sem chamar o
Google. Nunca loga o `id_token`, o e-mail nem o `sub`.
"""

import logging
from dataclasses import dataclass

import jwt
from fastapi import HTTPException
from fastapi import status as http_status

from app.core.auth import normalizar_email
from app.core.config import settings

URL_CHAVES_GOOGLE = "https://www.googleapis.com/oauth2/v3/certs"
EMISSORES_GOOGLE = ("accounts.google.com", "https://accounts.google.com")
ALGORITMO_GOOGLE = "RS256"
TIMEOUT_CHAVES_SEGUNDOS = 3
VALIDADE_CACHE_CHAVES_SEGUNDOS = 60 * 60
# O `iat` e o `exp` vêm do relógio do Google, não do nosso. Sem folga, um
# relógio do Railway um pouco atrasado recusaria o token recém-emitido
# ("iat no futuro"). Dez segundos não mudam nada num token que vale 1 hora.
FOLGA_RELOGIO_SEGUNDOS = 10


class GoogleIndisponivel(Exception):
    """`GOOGLE_CLIENT_ID` vazio ou falha ao buscar as chaves: 503."""


class ContaGoogleInvalida(Exception):
    """Token inválido, vencido, de outra audiência ou de outro emissor, ou
    e-mail não verificado: 401."""


@dataclass(frozen=True)
class IdentidadeGoogle:
    sub: str
    email: str
    nome: str | None


def _criar_cliente_de_chaves() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        URL_CHAVES_GOOGLE,
        cache_jwk_set=True,
        lifespan=VALIDADE_CACHE_CHAVES_SEGUNDOS,
        timeout=TIMEOUT_CHAVES_SEGUNDOS,
    )


# Um só por processo, para o cache valer entre requisições. Os testes trocam
# por um falso (tests/conftest.py).
_cliente_de_chaves = _criar_cliente_de_chaves()


def _chave_de_assinatura(id_token: str) -> jwt.PyJWK:
    try:
        return _cliente_de_chaves.get_signing_key_from_jwt(id_token)
    except jwt.PyJWKClientConnectionError as exc:
        raise GoogleIndisponivel("chaves_indisponiveis") from exc
    except (jwt.PyJWKSetError, ValueError) as exc:
        # Resposta do Google sem chaves usáveis ou que não é JSON.
        raise GoogleIndisponivel("chaves_indisponiveis") from exc
    except jwt.PyJWTError as exc:
        # Cabeçalho ilegível ou `kid` que não está entre as chaves do Google.
        raise ContaGoogleInvalida from exc


def conferir_id_token(id_token: str) -> IdentidadeGoogle:
    """A identidade do token, só se o Google a confirmou.

    Levanta `GoogleIndisponivel` ou `ContaGoogleInvalida`. Não toca no banco."""
    client_id = settings.google_client_id.strip()
    if not client_id:
        raise GoogleIndisponivel("desligado")

    chave = _chave_de_assinatura(id_token)
    try:
        claims = jwt.decode(
            id_token,
            chave.key,
            algorithms=[ALGORITMO_GOOGLE],
            audience=client_id,
            issuer=EMISSORES_GOOGLE,
            leeway=FOLGA_RELOGIO_SEGUNDOS,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]},
        )
    except jwt.PyJWTError as exc:
        raise ContaGoogleInvalida from exc

    sub, email = claims["sub"], claims["email"]
    if (
        claims.get("email_verified") is not True
        or not isinstance(sub, str)
        or not sub
        or not isinstance(email, str)
        or not email.strip()
    ):
        raise ContaGoogleInvalida
    nome = claims.get("name")
    nome = nome.strip() if isinstance(nome, str) else ""
    return IdentidadeGoogle(sub=sub, email=normalizar_email(email), nome=nome or None)


MENSAGEM_INDISPONIVEL = "Login pelo Google indisponível."
MENSAGEM_CONTA_INVALIDA = "Não foi possível confirmar a conta Google."


def identidade_ou_erro_http(id_token: str, logger: logging.Logger, evento: str) -> IdentidadeGoogle:
    """`conferir_id_token` para as rotas: 503 ou 401, com o resultado
    categorizado no log (`<evento> resultado=...`)."""
    try:
        return conferir_id_token(id_token)
    except GoogleIndisponivel as exc:
        logger.info("%s resultado=%s", evento, exc.args[0])
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=MENSAGEM_INDISPONIVEL
        ) from exc
    except ContaGoogleInvalida as exc:
        logger.info("%s resultado=token_invalido", evento)
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail=MENSAGEM_CONTA_INVALIDA
        ) from exc
