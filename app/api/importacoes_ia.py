"""Importação do relatório de lançamentos via IA (issue #158, ADR-013).

O usuário cola o prompt do Ledgr e o PDF do sistema de gestão na IA que já
usa e importa aqui o JSON que ela devolve. O Ledgr não chama IA nenhuma.
Regras da leitura em app/services/importacao_ia/leitura.py; do rascunho, em
app/services/importacao_ia/rascunho.py.

`GET /importacoes-ia/prompt` (rota da conta, qualquer papel): o texto do
prompt, `versao_schema`, `versao_prompt` e o JSON Schema. O texto muda sem
mudar o schema, e por isso as duas versões são separadas.

`POST /importacoes-ia` (por empresa, `X-Ledgr-Empresa`, qualquer papel):
multipart com `arquivo`, `.json` ou `.txt`, até 5MB (400 e 413 com as
mensagens do upload de extrato), 10 por minuto por conta, o limite do
upload. Diferente do upload, é síncrono: lê, valida e grava o rascunho na
própria requisição, porque o 422 precisa dizer tudo o que está errado.
Por isso é `def`, e o FastAPI a roda numa thread, sem prender o event loop
com o insert. Antes de gravar, apaga até 100 rascunhos vencidos de qualquer
conta, os mais antigos primeiro.

- `201`: `importacao_id` (o id do extrato), `status` "rascunho",
  `quantidade_lancamentos`, o período, `total_entradas` e `total_saidas` (a
  soma das positivas e a das negativas, em string) e `expira_em`, para a
  pessoa conferir com o total do relatório: um lançamento que a IA deixou de
  fora não aparece como erro.
- `422` com `detail` em texto, sempre. Fora do schema, também `erros` (até
  50, cada um com `caminho`, `lancamento` e `mensagem`) e `total_erros`.
  Arquivo cortado, vazio, fora de UTF-8, com texto fora do bloco ou JSON
  quebrado: só o `detail`.

`DELETE /importacoes-ia/{importacao_id}` (por empresa, qualquer papel)
descarta o rascunho: `204`, sem resíduo. Inexistente, de outra empresa,
vencido ou já confirmado: 404 "Importação não encontrada.".

A revisão (listar, corrigir, remover linhas e confirmar) é da issue #159.

Nunca loga o conteúdo do arquivo, a descrição nem os valores: só o id do
rascunho e contagens.
"""

import logging
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.extratos import TAMANHO_MAXIMO_BYTES
from app.core.auth import EmpresaAtiva, obter_empresa_ativa, obter_usuario_autenticado
from app.core.database import get_db
from app.core.rate_limit import LIMITE_UPLOAD, chave_por_conta, limiter
from app.models import Usuario
from app.services.importacao_ia import prompt_atual, schema_atual
from app.services.importacao_ia.leitura import (
    ARQUIVO_CORTADO,
    FORA_DO_SCHEMA,
    ArquivoCortado,
    ArquivoForaDoSchema,
    ArquivoIlegivel,
    ler_importacao,
)
from app.services.importacao_ia.rascunho import (
    VALIDADE,
    apagar_vencidos,
    criar_rascunho,
    descartar,
    rascunho_da_empresa,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/importacoes-ia", tags=["importacoes-ia"])

EXTENSOES_ACEITAS = {".json", ".txt"}
IMPORTACAO_NAO_ENCONTRADA = "Importação não encontrada."
ZERO = Decimal("0.00")


class ErroDeCampoResponse(BaseModel):
    caminho: str
    lancamento: int | None
    mensagem: str


class ImportacaoInvalidaResponse(BaseModel):
    detail: str
    erros: list[ErroDeCampoResponse] | None = None
    total_erros: int | None = None


class ImportacaoResponse(BaseModel):
    importacao_id: uuid.UUID
    status: str
    quantidade_lancamentos: int
    periodo_inicio: date
    periodo_fim: date
    total_entradas: Decimal
    total_saidas: Decimal
    expira_em: datetime


@router.get("/prompt")
def obter_prompt(
    _usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
) -> dict:
    prompt = prompt_atual()
    return {
        "versao_schema": prompt.versao_schema,
        "versao_prompt": prompt.versao_prompt,
        "prompt": prompt.texto,
        "schema": schema_atual(),
    }


def _invalida(detalhe: str, erro: ArquivoForaDoSchema | None = None) -> JSONResponse:
    """422 com `detail` em texto; fora do schema, também os erros por campo
    (com `lancamento` nulo nos erros da raiz)."""
    corpo = ImportacaoInvalidaResponse(detail=detalhe)
    if erro is not None:
        corpo.erros = [ErroDeCampoResponse(**vars(e)) for e in erro.erros]
        corpo.total_erros = erro.total
    return JSONResponse(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={chave: valor for chave, valor in corpo.model_dump().items() if valor is not None},
    )


@router.post(
    "",
    response_model=ImportacaoResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses={422: {"model": ImportacaoInvalidaResponse}},
)
@limiter.limit(LIMITE_UPLOAD, key_func=chave_por_conta)
def importar(
    request: Request,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    arquivo: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
):
    extensao = os.path.splitext(arquivo.filename or "")[1].lower()
    if extensao not in EXTENSOES_ACEITAS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Formato de arquivo não suportado. Envie o arquivo .json ou .txt com o que a "
            "IA devolveu.",
        )
    conteudo = arquivo.file.read(TAMANHO_MAXIMO_BYTES + 1)
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Arquivo excede o limite de 5MB.",
        )

    try:
        lancamentos = ler_importacao(conteudo)
    except ArquivoCortado:
        return _invalida(ARQUIVO_CORTADO)
    except ArquivoIlegivel as exc:
        return _invalida(exc.mensagem)
    except ArquivoForaDoSchema as exc:
        return _invalida(FORA_DO_SCHEMA, exc)

    vencidos = apagar_vencidos(db)
    rascunho = criar_rascunho(db, ativa.empresa.id, arquivo.filename, len(conteudo), lancamentos)
    db.commit()
    db.refresh(rascunho)
    logger.info(
        "importação via IA: rascunho %s com %d lançamentos; %d rascunhos vencidos apagados",
        rascunho.id,
        len(lancamentos),
        vencidos,
    )
    return ImportacaoResponse(
        importacao_id=rascunho.id,
        status=rascunho.status,
        quantidade_lancamentos=len(lancamentos),
        periodo_inicio=rascunho.periodo_inicio,
        periodo_fim=rascunho.periodo_fim,
        total_entradas=sum((item.valor for item in lancamentos if item.valor > 0), ZERO),
        total_saidas=sum((item.valor for item in lancamentos if item.valor < 0), ZERO),
        expira_em=(rascunho.criado_em + VALIDADE).replace(tzinfo=UTC),
    )


@router.delete("/{importacao_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def descartar_importacao(
    importacao_id: uuid.UUID,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    rascunho = rascunho_da_empresa(db, ativa.empresa.id, importacao_id, travar=True)
    if rascunho is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=IMPORTACAO_NAO_ENCONTRADA
        )
    descartar(db, rascunho)
    db.commit()
    logger.info("importação via IA: rascunho %s descartado", importacao_id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
