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

Dados da empresa (issue #223, ADR-028). Com `ASSISTENTE_DADOS_HABILITADO` e um
`X-Ledgr-Empresa` (validado como nas outras rotas: 400 ou 404), o assistente
também responde consultas simples sobre a empresa ativa. Duas regras:

- nenhum dado sensível vai à IA. A pergunta e o histórico saem com marcadores
  de valor e data (app/services/ajuda/marcadores.py), além da máscara, e nos
  turnos do assistente todo número vira `[N]`. O modelo só devolve o tipo da
  pergunta e, nas de dados, a consulta; o backend responde com o banco e
  textos prontos (app/services/ajuda/consultas.py), e o resultado nunca volta
  ao provedor. Uma chamada ao provedor por pergunta, como antes;
- o assistente nunca grava dados. Pedido de gravar ("acao") recebe a recusa
  fixa com a tela onde a pessoa faz aquilo, e a consulta roda numa transação
  `READ ONLY` do Postgres. Falha da consulta: 503, sem gravar uso.

Os links passam a ter `tipo`: "ajuda" (seção da Central) ou "tela" (caminho
do app sem o prefixo `/e/<empresa>`, de uma lista fechada). Sem a flag, ou sem
o cabeçalho, a rota é a do ADR-020: ignora a empresa e manda a pergunta só
com a máscara.
"""

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.auth import CABECALHO_EMPRESA, empresa_do_cabecalho, obter_usuario_autenticado
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import LIMITE_ASSISTENTE_POR_USUARIO, chave_por_usuario, limiter
from app.models import Usuario
from app.services.ajuda import consultas, uso
from app.services.ajuda.conteudo import (
    ConteudoGrandeDemais,
    ConteudoIndisponivel,
    FonteDaAjuda,
    caminho,
    obter_fonte_da_ajuda,
    serializar,
)
from app.services.ajuda.marcadores import marcar_conversa
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
RECUSA_DE_ACAO = "Pelo assistente eu só consulto e não gravo nada."
FORA_DO_QUE_SEI = "Ainda não sei responder isso. Consulte a Central de ajuda."


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
    # "ajuda": seção da Central (`/ajuda/<pagina>#<funcao>`); "tela": caminho
    # do app sem o prefixo `/e/<empresa>`, que o front acrescenta (issue #223).
    tipo: Literal["ajuda", "tela"] = "ajuda"


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


def _links_da_central(central, citacoes) -> list[LinkAjuda]:
    links: list[LinkAjuda] = []
    for pagina_id, funcao_id in citacoes:
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
    return links


def _links_de_tela(links: tuple[consultas.LinkDeTela, ...]) -> list[LinkAjuda]:
    return [
        LinkAjuda(titulo=link.titulo, caminho=link.caminho, tipo=link.tipo)
        for link in links[:MAXIMO_LINKS]
    ]


@router.post("/perguntas", response_model=RespostaAssistente)
@limiter.limit(LIMITE_ASSISTENTE_POR_USUARIO, key_func=chave_por_usuario)
def perguntar(
    request: Request,
    corpo: PerguntaRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeIA | None, Depends(obter_provedor_dependencia)],
    fonte: Annotated[FonteDaAjuda, Depends(obter_fonte_da_ajuda)],
    x_ledgr_empresa: Annotated[str | None, Header(alias=CABECALHO_EMPRESA)] = None,
) -> RespostaAssistente:
    if not (settings.assistente_habilitado and settings.llm_habilitado) or provedor is None:
        logger.info("assistente resultado=desligado")
        raise _indisponivel()

    empresa = (
        empresa_do_cabecalho(db, usuario, x_ledgr_empresa)
        if settings.assistente_dados_habilitado and x_ledgr_empresa is not None
        else None
    )
    empresa_id = empresa.id if empresa is not None else None
    nomes_da_conta = (
        consultas.nomes_das_empresas_da_conta(db, usuario.conta_id) if empresa_id else ()
    )

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

    tela = corpo.tela if corpo.tela and central.tem_pagina(corpo.tela) else None
    mes_atual = None
    if empresa_id is not None:
        mes_atual = consultas.mes_atual_de_sao_paulo(datetime.now(UTC))
        marcada = marcar_conversa(
            corpo.pergunta,
            [(turno.papel, turno.texto) for turno in corpo.historico],
            nomes_da_conta,
        )
        pergunta_enviada, historico = marcada.pergunta, marcada.historico
    else:
        pergunta_enviada = mascarar_texto(corpo.pergunta)
        historico = tuple(
            MensagemHistorico(papel=turno.papel, texto=mascarar_texto(turno.texto))
            for turno in corpo.historico
        )
    pergunta = PerguntaAjuda(
        central=central_serializada,
        pergunta=pergunta_enviada,
        historico=historico,
        tela=tela,
        dados=empresa_id is not None,
        mes_atual=mes_atual,
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

    if resposta.tipo == "dados" and empresa_id is not None and resposta.consulta is not None:
        consulta = resposta.consulta
        try:
            resultado = consultas.executar_somente_leitura(
                db, lambda: consultas.responder(db, empresa_id, consulta, mes_atual)
            )
        except Exception as exc:
            logger.info("assistente resultado=erro_consulta classe=%s", type(exc).__name__)
            raise _indisponivel() from exc
        texto, respondida = resultado.texto, True
        links = _links_de_tela(resultado.links)
        categoria_do_log = f"dados intencao={consulta.intencao}"
    elif resposta.tipo == "acao":
        destino = consultas.TELAS_DE_ACAO.get(resposta.tela or "")
        texto, respondida = RECUSA_DE_ACAO, True
        links = []
        if destino is not None:
            texto += f" Para isso, use a tela {destino.titulo}."
            links = _links_de_tela((destino,))
        categoria_do_log = "acao_recusada"
    elif resposta.tipo in ("dados", "fora"):
        texto, respondida, links = FORA_DO_QUE_SEI, False, []
        categoria_do_log = "fora"
    else:
        texto, respondida = _texto_puro(resposta.resposta), resposta.respondida
        links = _links_da_central(central, resposta.citacoes)
        categoria_do_log = "respondida" if respondida else "nao_respondida"

    try:
        uso.registrar_uso(db, conta_id)
        if not respondida:
            uso.registrar_sem_resposta(db, pergunta_enviada, corpo.tela)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - a resposta já foi paga: entrega mesmo assim
        db.rollback()
        logger.info("assistente registro_falhou classe=%s", type(exc).__name__)

    logger.info("assistente resultado=%s links=%d", categoria_do_log, len(links))
    return RespostaAssistente(resposta=texto, respondida=respondida, links=links)
