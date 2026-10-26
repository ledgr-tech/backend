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

O acesso ao R2 fica atrás do `Armazenamento`, que os testes trocam por um em
memória. `obter_arquivo_original` é a dependency das rotas: devolve `None`
quando falta alguma variável do R2 ou a chave é inválida, e quem chama decide
o que isso significa (o upload segue sem guardar, o download responde 503). O
cliente do R2 é criado aqui, e não importado de scripts/backup_r2.py: a imagem
do backup não leva o código da API, e a API não depende de script.

Com a flag desligada, `obter_arquivo_original` nem cria o cliente: uma
variável do R2 mal digitada em produção, antes de o recurso ser ligado, não
pode derrubar o `/me` e o upload de todas as contas. Um cliente que o `boto3`
recusa (endpoint sem `https://`, por exemplo) conta como não configurado.

No log, só o nome da classe da exceção, aqui e em quem chama: a mensagem de um
erro do `boto3` traz o endpoint e o bucket.
"""

import base64
import binascii
import functools
import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

VERSAO_CIFRA = 1
TAMANHO_NONCE = 12
TAMANHO_CHAVE_CIFRA = 32
# A tag do GCM vem no fim do texto cifrado.
TAMANHO_TAG = 16
# O DeleteObjects da API S3 aceita até mil chaves por chamada.
LOTE_DE_EXCLUSAO = 1000
# O R2 responde NoSuchKey no GetObject de objeto que não existe; "404" é o que
# o botocore usa quando a resposta vem sem corpo.
CODIGOS_DE_OBJETO_INEXISTENTE = frozenset({"NoSuchKey", "404"})


class ArquivoOriginalCorrompido(ValueError):
    """O objeto não abre com a chave: outra chave de cifra, outro objeto, versão
    desconhecida ou dados estragados."""


class ArquivoOriginalNaoEncontrado(LookupError):
    """O objeto não está no bucket (por exemplo, depois de um restore do banco)."""


class Armazenamento(Protocol):
    def enviar(self, chave_objeto: str, dados: bytes) -> None: ...

    def baixar(self, chave_objeto: str) -> bytes:
        """Levanta `ArquivoOriginalNaoEncontrado` se o objeto não existe."""
        ...

    def apagar(self, chave_objeto: str) -> None: ...

    def contar_prefixo(self, prefixo: str) -> int: ...

    def apagar_prefixo(self, prefixo: str) -> int:
        """Apaga todos os objetos do prefixo e devolve quantos apagou."""
        ...


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


def cliente_r2(endpoint: str, chave_id: str, segredo: str):
    """Cliente S3 do `boto3` para o R2. Os timeouts são curtos de propósito: o
    upload espera o envio, e um R2 lento não pode prender a requisição. O
    checksum fica só quando a operação exige, como no backup (o R2 não aceita
    os checksums novos que o boto3 manda por padrão)."""
    import boto3
    from botocore.config import Config

    # Sessão própria: a sessão padrão do boto3 não é thread-safe, e a
    # dependency das rotas síncronas roda no threadpool.
    return boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=chave_id,
        aws_secret_access_key=segredo,
        config=Config(
            region_name="auto",
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


class ArmazenamentoR2:
    """O `Armazenamento` no bucket do R2, pelo cliente de `cliente_r2`."""

    def __init__(self, cliente, bucket: str):
        self._cliente = cliente
        self._bucket = bucket

    def enviar(self, chave_objeto: str, dados: bytes) -> None:
        self._cliente.put_object(Bucket=self._bucket, Key=chave_objeto, Body=dados)

    def baixar(self, chave_objeto: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            resposta = self._cliente.get_object(Bucket=self._bucket, Key=chave_objeto)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in CODIGOS_DE_OBJETO_INEXISTENTE:
                raise ArquivoOriginalNaoEncontrado("objeto não está no bucket") from None
            raise
        return resposta["Body"].read()

    def apagar(self, chave_objeto: str) -> None:
        self._cliente.delete_object(Bucket=self._bucket, Key=chave_objeto)

    def _paginas(self, prefixo: str) -> Iterator[list[str]]:
        paginador = self._cliente.get_paginator("list_objects_v2")
        for pagina in paginador.paginate(Bucket=self._bucket, Prefix=prefixo):
            yield [objeto["Key"] for objeto in pagina.get("Contents", [])]

    def contar_prefixo(self, prefixo: str) -> int:
        return sum(len(chaves) for chaves in self._paginas(prefixo))

    def apagar_prefixo(self, prefixo: str) -> int:
        apagados = 0
        for chaves in self._paginas(prefixo):
            for inicio in range(0, len(chaves), LOTE_DE_EXCLUSAO):
                lote = chaves[inicio : inicio + LOTE_DE_EXCLUSAO]
                resposta = self._cliente.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": chave} for chave in lote], "Quiet": True},
                )
                if resposta.get("Errors"):
                    raise RuntimeError(f"o R2 não apagou {len(resposta['Errors'])} objeto(s)")
                apagados += len(lote)
        return apagados


@dataclass(frozen=True)
class ArquivoOriginal:
    """Cifra e guarda, baixa e decifra, apaga: o que as rotas usam."""

    armazenamento: Armazenamento
    chave_cifra: bytes = field(repr=False)

    def guardar(self, chave_objeto: str, conteudo: bytes) -> None:
        self.armazenamento.enviar(chave_objeto, cifrar(self.chave_cifra, chave_objeto, conteudo))

    def abrir(self, chave_objeto: str) -> bytes:
        return decifrar(self.chave_cifra, chave_objeto, self.armazenamento.baixar(chave_objeto))

    def apagar(self, chave_objeto: str) -> None:
        self.armazenamento.apagar(chave_objeto)


def _armazenamento(
    endpoint: str, bucket: str, chave_id: str, segredo: str
) -> ArmazenamentoR2 | None:
    valores = [valor.strip() for valor in (endpoint, bucket, chave_id, segredo)]
    if not all(valores):
        return None
    endpoint, bucket, chave_id, segredo = valores
    try:
        cliente = cliente_r2(endpoint, chave_id, segredo)
    except Exception as exc:  # noqa: BLE001 - variável mal formada conta como não configurada
        logger.warning("cliente do R2 do arquivo original não foi criado (%s)", type(exc).__name__)
        return None
    return ArmazenamentoR2(cliente, bucket)


def _arquivo_original(
    endpoint: str, bucket: str, chave_id: str, segredo: str, chave: str
) -> ArquivoOriginal | None:
    chave_cifra = ler_chave_de_cifra(chave)
    if chave_cifra is None:
        return None
    armazenamento = _armazenamento(endpoint, bucket, chave_id, segredo)
    if armazenamento is None:
        return None
    return ArquivoOriginal(armazenamento, chave_cifra)


def armazenamento_do_ambiente(cfg: Settings) -> ArmazenamentoR2 | None:
    """Só o R2, sem a chave de cifra: é o que o script de exclusão precisa."""
    return _armazenamento(
        cfg.arquivo_original_r2_endpoint,
        cfg.arquivo_original_r2_bucket,
        cfg.arquivo_original_r2_access_key_id,
        cfg.arquivo_original_r2_secret_access_key,
    )


def arquivo_original_do_ambiente(cfg: Settings) -> ArquivoOriginal | None:
    return _arquivo_original(*_valores(cfg))


def _valores(cfg: Settings) -> tuple[str, str, str, str, str]:
    return (
        cfg.arquivo_original_r2_endpoint,
        cfg.arquivo_original_r2_bucket,
        cfg.arquivo_original_r2_access_key_id,
        cfg.arquivo_original_r2_secret_access_key,
        cfg.arquivo_original_chave,
    )


# Uma instância por configuração: o cliente do boto3 não é recriado a cada
# requisição, e trocar as variáveis (nos testes) monta outra.
_arquivo_original_em_cache = functools.lru_cache(maxsize=1)(_arquivo_original)


def obter_arquivo_original() -> ArquivoOriginal | None:
    """Dependency das rotas. `None` com a flag `ARQUIVO_ORIGINAL_HABILITADO`
    desligada ou quando o R2 ou a chave não estão configurados."""
    if not settings.arquivo_original_habilitado:
        return None
    return _arquivo_original_em_cache(*_valores(settings))


def conta_guarda_o_original(conta, servico: ArquivoOriginal | None) -> bool:
    """O upload desta conta guarda o original agora? Flag ligada, R2 e chave
    configurados e o plano da conta entre os que guardam. É o mesmo teste do
    upload e do `guarda_arquivo_original` do `GET /me`, para os dois nunca
    divergirem."""
    from app.core.plano import guarda_arquivo_original

    return (
        settings.arquivo_original_habilitado
        and servico is not None
        and guarda_arquivo_original(conta)
    )


def original_visivel(arquivo_chave: str | None) -> bool:
    """O `arquivo_original` da listagem, do detalhe e do upload: há original
    guardado e a flag está ligada. Não olha o plano atual: quem saiu do
    avançado continua baixando o que já foi guardado (ADR-023)."""
    return settings.arquivo_original_habilitado and arquivo_chave is not None
