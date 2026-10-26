"""Arquivo original do extrato no Cloudflare R2 (issue #139, ADR-023).

Nas contas do plano `avancado`, com `ARQUIVO_ORIGINAL_HABILITADO` ligada, o
upload guarda o arquivo que a pessoa enviou, e o admin e o gestor baixam o
original depois. Nos outros planos, o ADR-002 continua valendo: o arquivo é
descartado depois do processamento.

O backend cifra o arquivo antes de enviar (AES-256-GCM, chave em
`ARQUIVO_ORIGINAL_CHAVE`), e a Cloudflare só vê o arquivo cifrado, como no
backup do ADR-018. O objeto guarda um byte de versão, o nonce aleatório e o
texto cifrado com a tag. A chave do objeto entra como dado associado: um
arquivo copiado para a chave de outro extrato não abre.

A chave do objeto é `contas/<conta_id>/empresas/<empresa_id>/<extrato_id>`,
sem o nome do arquivo, que pode ter nome de pessoa. O prefixo da conta é o que
`scripts/excluir_empresa.py` apaga.
"""

import base64
import binascii
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSAO_CIFRA = 1
TAMANHO_NONCE = 12
TAMANHO_CHAVE_CIFRA = 32
# A tag do GCM vem no fim do texto cifrado.
TAMANHO_TAG = 16


class ArquivoOriginalCorrompido(ValueError):
    """O objeto não abre com a chave: outra chave de cifra, outro objeto, versão
    desconhecida ou dados estragados."""


def chave_do_objeto(conta_id: uuid.UUID, empresa_id: uuid.UUID, extrato_id: uuid.UUID) -> str:
    return f"{prefixo_da_conta(conta_id)}empresas/{empresa_id}/{extrato_id}"


def prefixo_da_conta(conta_id: uuid.UUID) -> str:
    return f"contas/{conta_id}/"


def ler_chave_de_cifra(valor: str) -> bytes | None:
    """A chave de `ARQUIVO_ORIGINAL_CHAVE`: 32 bytes em base64. Qualquer outra
    coisa devolve `None`, e o recurso conta como não configurado."""
    try:
        chave = base64.b64decode(valor.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return chave if len(chave) == TAMANHO_CHAVE_CIFRA else None


def cifrar(chave: bytes, chave_objeto: str, conteudo: bytes) -> bytes:
    nonce = os.urandom(TAMANHO_NONCE)
    cifrado = AESGCM(chave).encrypt(nonce, conteudo, chave_objeto.encode())
    return bytes([VERSAO_CIFRA]) + nonce + cifrado


def decifrar(chave: bytes, chave_objeto: str, dados: bytes) -> bytes:
    if len(dados) < 1 + TAMANHO_NONCE + TAMANHO_TAG or dados[0] != VERSAO_CIFRA:
        raise ArquivoOriginalCorrompido("arquivo original em formato desconhecido")
    nonce, cifrado = dados[1 : 1 + TAMANHO_NONCE], dados[1 + TAMANHO_NONCE :]
    try:
        return AESGCM(chave).decrypt(nonce, cifrado, chave_objeto.encode())
    except InvalidTag as exc:
        raise ArquivoOriginalCorrompido("arquivo original não abre com a chave") from exc
