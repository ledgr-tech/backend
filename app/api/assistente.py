"""Assistente da Central de ajuda (issue #137, ADR-020).

`POST /assistente/perguntas` responde "como faço tal coisa no Ledgr" só com
base na Central de ajuda que o front publica (app/services/ajuda/conteudo.py),
com o passo a passo e os links das seções citadas. Rota da conta: ignora
`X-Ledgr-Empresa`. Disponível em todos os planos e para os três papéis,
inclusive no teste encerrado, porque a pergunta não leva dado do cliente.

Ordem:

1. `ASSISTENTE_HABILITADO` e `LLM_HABILITADO` ligadas e provedor configurado;
   senão, 503 "O assistente está indisponível no momento.".
2. Limite diário da conta e global (app/services/ajuda/uso.py), separados dos
   das explicações: 429 sem chamar o provedor.
3. A transação é encerrada antes do que é externo (a busca do conteúdo e a
   chamada ao provedor), como nas explicações.
4. Conteúdo da Central: sem cópia válida, ou grande demais para o contexto,
   503 com a mesma mensagem.
5. A pergunta e o histórico passam pela máscara de identificadores do ADR-011
   (modo "mascarada"). Nenhum dado de lançamento, da empresa ou da conta vai
   ao provedor; o identificador anônimo é o sha256 do `conta_id`.
6. Falha do provedor (inclusive saída que não é o JSON combinado) ou erro
   inesperado: 503, sem gravar uso.
7. Os links saem só das citações que existem no conteúdo atual (título da
   função e `/ajuda/<pagina>#<funcao>`), sem repetir, no máximo `MAXIMO_LINKS`.
8. Grava o uso. Com `respondida` falsa, grava também a pergunta já mascarada e
   a tela, sem usuário, conta nem empresa.

O backend não guarda conversa: o histórico vem do front a cada pergunta.
Rate limit de `LIMITE_ASSISTENTE_POR_USUARIO` por usuário. Nunca loga a
pergunta, o histórico nem a resposta: só o resultado categorizado.
"""

import hashlib
import logging
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.auth import obter_usuario_autenticado
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import LIMITE_ASSISTENTE_POR_USUARIO, chave_por_usuario, limiter
from app.models import Usuario
from app.services.ajuda import uso
from app.services.ajuda.conteudo import (
    ConteudoGrandeDemais,
    ConteudoIndisponivel,
    FonteDaAjuda,
    caminho,
    obter_fonte_da_ajuda,
    serializar,
)
from app.services.ia import MensagemHistorico, PerguntaAjuda, ProvedorDeIA, ProvedorIAIndisponivel
from app.services.ia.geracao import obter_provedor_dependencia
from app.services.ia.mascaramento import mascarar_texto

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistente", tags=["assistente"])

INDISPONIVEL = "O assistente está indisponível no momento."
LIMITE_DE_HOJE = (
    "Você chegou ao limite de perguntas de hoje. Tente amanhã ou consulte a Central de ajuda."
)
TAMANHO_MAXIMO_PERGUNTA = 500
TAMANHO_MAXIMO_TURNO = 1000
MAXIMO_TURNOS = 6
MAXIMO_LINKS = 3


class TurnoHistorico(BaseModel):
    papel: Literal["usuario", "assistente"]
    texto: Annotated[str, Field(min_length=1, max_length=TAMANHO_MAXIMO_TURNO)]


class PerguntaRequest(BaseModel):
    pergunta: str
    tela: Annotated[str | None, Field(max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")] = None
    historico: Annotated[list[TurnoHistorico], Field(max_length=MAXIMO_TURNOS)] = []

    @field_validator("pergunta")
    @classmethod
    def _sem_espacos_nas_pontas(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("não pode ser vazia")
        if len(valor) > TAMANHO_MAXIMO_PERGUNTA:
            raise ValueError(f"pode ter até {TAMANHO_MAXIMO_PERGUNTA} caracteres")
        return valor


class LinkAjuda(BaseModel):
    titulo: str
    caminho: str


class RespostaAssistente(BaseModel):
    resposta: str
    respondida: bool
    links: list[LinkAjuda]


def _indisponivel() -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=INDISPONIVEL)


# O que sobra de markdown além dos passos numerados: negrito, código e títulos.
_MARCAS_DE_MARKDOWN = re.compile(r"\*\*|__|`")
_TITULO_DE_MARKDOWN = re.compile(r"^\s*#+\s*", re.MULTILINE)


def _texto_puro(resposta: str) -> str:
    return _TITULO_DE_MARKDOWN.sub("", _MARCAS_DE_MARKDOWN.sub("", resposta)).strip()


@router.post("/perguntas", response_model=RespostaAssistente)
@limiter.limit(LIMITE_ASSISTENTE_POR_USUARIO, key_func=chave_por_usuario)
def perguntar(
    request: Request,
    corpo: PerguntaRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeIA | None, Depends(obter_provedor_dependencia)],
    fonte: Annotated[FonteDaAjuda, Depends(obter_fonte_da_ajuda)],
) -> RespostaAssistente:
    if not (settings.assistente_habilitado and settings.llm_habilitado) or provedor is None:
        logger.info("assistente resultado=desligado")
        raise _indisponivel()

    conta_id = usuario.conta_id
    limite = uso.limite_atingido(db, conta_id)
    if limite is not None:
        db.rollback()
        logger.info("assistente resultado=limite_%s", limite)
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS, detail=LIMITE_DE_HOJE
        )
    # Nenhuma transação aberta durante a busca do conteúdo e a chamada ao provedor.
    db.rollback()

    try:
        central = fonte.obter()
        central_serializada = serializar(central, settings.assistente_maximo_conteudo_caracteres)
    except (ConteudoIndisponivel, ConteudoGrandeDemais) as exc:
        logger.info("assistente resultado=sem_conteudo motivo=%s", type(exc).__name__)
        raise _indisponivel() from exc

    pergunta_mascarada = mascarar_texto(corpo.pergunta)
    pergunta = PerguntaAjuda(
        central=central_serializada,
        pergunta=pergunta_mascarada,
        historico=tuple(
            MensagemHistorico(papel=turno.papel, texto=mascarar_texto(turno.texto))
            for turno in corpo.historico
        ),
        tela=corpo.tela if corpo.tela and central.tem_pagina(corpo.tela) else None,
    )
    try:
        resposta = provedor.responder_ajuda(
            pergunta, identificador_anonimo=hashlib.sha256(str(conta_id).encode()).hexdigest()
        )
    except ProvedorIAIndisponivel as exc:
        nivel = logging.WARNING if exc.motivo == "cota_esgotada" else logging.INFO
        logger.log(nivel, "assistente resultado=erro_provedor motivo=%s", exc.motivo)
        raise _indisponivel() from exc
    except Exception as exc:
        logger.info("assistente resultado=erro_inesperado classe=%s", type(exc).__name__)
        raise _indisponivel() from exc

    links: list[LinkAjuda] = []
    for pagina_id, funcao_id in resposta.citacoes:
        funcao = central.funcao(pagina_id, funcao_id)
        link = (
            LinkAjuda(titulo=funcao.titulo, caminho=caminho(pagina_id, funcao_id))
            if funcao
            else None
        )
        if link is not None and link not in links:
            links.append(link)
        if len(links) == MAXIMO_LINKS:
            break

    try:
        uso.registrar_uso(db, conta_id)
        if not resposta.respondida:
            uso.registrar_sem_resposta(db, pergunta_mascarada, corpo.tela)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - a resposta já foi paga: entrega mesmo assim
        db.rollback()
        logger.info("assistente registro_falhou classe=%s", type(exc).__name__)

    logger.info(
        "assistente resultado=%s links=%d",
        "respondida" if resposta.respondida else "nao_respondida",
        len(links),
    )
    return RespostaAssistente(
        resposta=_texto_puro(resposta.resposta), respondida=resposta.respondida, links=links
    )
