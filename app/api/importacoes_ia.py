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

Revisão (issue #159), por empresa e para todos os papéis, sob
`/importacoes-ia/{importacao_id}`:

- `GET`: o resumo (quantidade, `total_entradas`, `total_saidas` negativa,
  período e `expira_em`) e as linhas na ordem de `posicao`, paginadas como
  `GET /conciliacoes/{id}` (`limit` de 1 a 1000, padrão 100), cada uma com
  `id`, `numero` (a partir de 1, na ordem atual), `data`, `valor` e
  `descricao`.
- `PATCH /lancamentos/{lancamento_id}`: corrige `data`, `valor` e/ou
  `descricao` com as validações e as mensagens do schema v1; `POST
  /lancamentos` acrescenta uma linha no fim da ordem, com os três. As duas
  devolvem a linha e o resumo; o `422` tem o formato do da importação, com o
  nome do campo no caminho. `DELETE /lancamentos/{lancamento_id}`: `204`,
  mesmo que o rascunho fique sem linha.
- `POST /confirmar`: renumera, passa a extrato "concluido", grava quem
  confirmou e quando e devolve o corpo de `GET /extratos/{id}`. Sem linha:
  `422`.

As escritas têm `LIMITE_REVISAO_IMPORTACAO_POR_USUARIO` por usuário e travam
o rascunho (`FOR UPDATE`): uma correção, a confirmação e o descarte nunca
correm um contra o outro. Rascunho inexistente, de outra empresa ou vencido
(inclusive no meio da revisão): 404 "Importação não encontrada.". Importação
já confirmada: 409 "Esta importação já foi confirmada." em todas as rotas da
revisão, para o duplo clique. O descarte segue com 404 (contrato da #158).
Linha que não é do rascunho: 404 "Lançamento não encontrado.".

Nunca loga o conteúdo do arquivo, a descrição nem os valores: só o id do
rascunho e contagens.
"""

import logging
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.extratos import TAMANHO_MAXIMO_BYTES, ExtratoDetalheResponse, detalhe_do_extrato
from app.core.auth import EmpresaAtiva, obter_empresa_ativa, obter_usuario_autenticado
from app.core.database import get_db
from app.core.rate_limit import (
    LIMITE_REVISAO_IMPORTACAO_POR_USUARIO,
    LIMITE_UPLOAD,
    chave_por_conta,
    chave_por_usuario,
    limiter,
)
from app.models import Extrato, Lancamento, Usuario
from app.services.importacao_ia import prompt_atual, schema_atual
from app.services.importacao_ia.leitura import (
    ARQUIVO_CORTADO,
    FORA_DO_SCHEMA,
    ArquivoCortado,
    ArquivoForaDoSchema,
    ArquivoIlegivel,
    ler_campos,
    ler_importacao,
)
from app.services.importacao_ia.rascunho import (
    VALIDADE,
    Resumo,
    adicionar,
    apagar_vencidos,
    confirmar,
    corrigir,
    criar_rascunho,
    descartar,
    importacao_confirmada,
    lancamento_do_rascunho,
    numero_do_lancamento,
    pagina,
    rascunho_da_empresa,
    remover,
    resumo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/importacoes-ia", tags=["importacoes-ia"])

EXTENSOES_ACEITAS = {".json", ".txt"}
IMPORTACAO_NAO_ENCONTRADA = "Importação não encontrada."
IMPORTACAO_CONFIRMADA = "Esta importação já foi confirmada."
LANCAMENTO_NAO_ENCONTRADO = "Lançamento não encontrado."
LANCAMENTO_FORA_DO_SCHEMA = "O lançamento não está no formato do Ledgr. Corrija os pontos abaixo."
SEM_LANCAMENTOS = (
    "A importação não tem nenhum lançamento. Adicione um lançamento ou descarte a importação."
)
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


# --- revisão (issue #159) -------------------------------------------------------------


class ResumoResponse(BaseModel):
    quantidade: int
    total_entradas: Decimal
    total_saidas: Decimal
    periodo_inicio: date | None
    periodo_fim: date | None
    expira_em: datetime


class LancamentoRascunhoResponse(BaseModel):
    id: uuid.UUID
    numero: int
    data: date
    valor: Decimal
    descricao: str


class RevisaoResponse(BaseModel):
    importacao_id: uuid.UUID
    status: str
    resumo: ResumoResponse
    total: int
    limit: int
    offset: int
    itens: list[LancamentoRascunhoResponse]


class LinhaRevisadaResponse(BaseModel):
    lancamento: LancamentoRascunhoResponse
    resumo: ResumoResponse


def _resumo(valor: Resumo) -> ResumoResponse:
    return ResumoResponse(**vars(valor))


def _linha(numero: int, lancamento: Lancamento) -> LancamentoRascunhoResponse:
    return LancamentoRascunhoResponse(
        id=lancamento.id,
        numero=numero,
        data=lancamento.data,
        valor=lancamento.valor,
        descricao=lancamento.descricao,
    )


def _rascunho(
    db: Session, empresa_id: uuid.UUID, importacao_id: uuid.UUID, *, travar: bool = False
) -> Extrato:
    """O rascunho vigente, ou 409 se a importação já foi confirmada, ou 404."""
    rascunho = rascunho_da_empresa(db, empresa_id, importacao_id, travar=travar)
    if rascunho is not None:
        return rascunho
    if importacao_confirmada(db, empresa_id, importacao_id):
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=IMPORTACAO_CONFIRMADA)
    raise HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND, detail=IMPORTACAO_NAO_ENCONTRADA
    )


def _lancamento(db: Session, rascunho: Extrato, lancamento_id: uuid.UUID) -> Lancamento:
    lancamento = lancamento_do_rascunho(db, rascunho, lancamento_id)
    if lancamento is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=LANCAMENTO_NAO_ENCONTRADO
        )
    return lancamento


@router.get("/{importacao_id}", response_model=RevisaoResponse)
def revisar(
    importacao_id: uuid.UUID,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RevisaoResponse:
    rascunho = _rascunho(db, ativa.empresa.id, importacao_id)
    total, linhas = pagina(db, rascunho, limit, offset)
    return RevisaoResponse(
        importacao_id=rascunho.id,
        status=rascunho.status,
        resumo=_resumo(resumo(db, rascunho)),
        total=total,
        limit=limit,
        offset=offset,
        itens=[_linha(numero, lancamento) for numero, lancamento in linhas],
    )


@router.patch(
    "/{importacao_id}/lancamentos/{lancamento_id}",
    response_model=LinhaRevisadaResponse,
    responses={422: {"model": ImportacaoInvalidaResponse}},
)
@limiter.limit(LIMITE_REVISAO_IMPORTACAO_POR_USUARIO, key_func=chave_por_usuario)
def corrigir_lancamento(
    request: Request,
    importacao_id: uuid.UUID,
    lancamento_id: uuid.UUID,
    corpo: Annotated[dict, Body()],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
):
    rascunho = _rascunho(db, ativa.empresa.id, importacao_id, travar=True)
    lancamento = _lancamento(db, rascunho, lancamento_id)
    numero = numero_do_lancamento(db, lancamento)
    try:
        campos = ler_campos(corpo, todos=False, numero=numero)
    except ArquivoForaDoSchema as exc:
        return _invalida(LANCAMENTO_FORA_DO_SCHEMA, exc)
    corrigir(db, rascunho, lancamento, campos)
    db.commit()
    db.refresh(lancamento)
    logger.info("importação via IA: lançamento corrigido no rascunho %s", rascunho.id)
    return LinhaRevisadaResponse(
        lancamento=_linha(numero, lancamento), resumo=_resumo(resumo(db, rascunho))
    )


@router.delete(
    "/{importacao_id}/lancamentos/{lancamento_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
@limiter.limit(LIMITE_REVISAO_IMPORTACAO_POR_USUARIO, key_func=chave_por_usuario)
def remover_lancamento(
    request: Request,
    importacao_id: uuid.UUID,
    lancamento_id: uuid.UUID,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    rascunho = _rascunho(db, ativa.empresa.id, importacao_id, travar=True)
    remover(db, _lancamento(db, rascunho, lancamento_id))
    db.commit()
    logger.info("importação via IA: lançamento removido do rascunho %s", rascunho.id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{importacao_id}/lancamentos",
    response_model=LinhaRevisadaResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses={422: {"model": ImportacaoInvalidaResponse}},
)
@limiter.limit(LIMITE_REVISAO_IMPORTACAO_POR_USUARIO, key_func=chave_por_usuario)
def adicionar_lancamento(
    request: Request,
    importacao_id: uuid.UUID,
    corpo: Annotated[dict, Body()],
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
):
    rascunho = _rascunho(db, ativa.empresa.id, importacao_id, travar=True)
    try:
        campos = ler_campos(corpo, todos=True)
    except ArquivoForaDoSchema as exc:
        return _invalida(LANCAMENTO_FORA_DO_SCHEMA, exc)
    lancamento = adicionar(db, rascunho, campos)
    db.commit()
    db.refresh(lancamento)
    logger.info("importação via IA: lançamento acrescentado ao rascunho %s", rascunho.id)
    return LinhaRevisadaResponse(
        lancamento=_linha(numero_do_lancamento(db, lancamento), lancamento),
        resumo=_resumo(resumo(db, rascunho)),
    )


@router.post("/{importacao_id}/confirmar", response_model=ExtratoDetalheResponse)
@limiter.limit(LIMITE_REVISAO_IMPORTACAO_POR_USUARIO, key_func=chave_por_usuario)
def confirmar_importacao(
    request: Request,
    importacao_id: uuid.UUID,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> ExtratoDetalheResponse:
    rascunho = _rascunho(db, ativa.empresa.id, importacao_id, travar=True)
    if resumo(db, rascunho).quantidade == 0:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=SEM_LANCAMENTOS
        )
    confirmar(db, rascunho, ativa.usuario)
    db.commit()
    db.refresh(rascunho)
    logger.info(
        "importação via IA: rascunho %s confirmado com %d lançamentos",
        rascunho.id,
        rascunho.quantidade_lancamentos,
    )
    return detalhe_do_extrato(db, rascunho)
