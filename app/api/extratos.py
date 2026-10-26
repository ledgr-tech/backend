"""Endpoints de extrato: upload (issue #7) e relatório de validação (issue #13).

`POST /extratos/upload` exige o Form field `origem` ("banco" ou "sistema",
issue #17), sem default: ausente ou inválido devolve 422 do FastAPI. Valida
extensão (.ofx/.csv, ver ADR-001) e tamanho
(5MB), persiste só os metadados do extrato — nunca o conteúdo do arquivo
(ADR-002) — e retorna `extrato_id` e `status="pendente"` imediatamente, sem
esperar o parsing.

Desde a issue #12, o conteúdo lido em memória é passado pra BackgroundTask
de normalização (`app.services.normalizacao.normalizar_extrato`), agendada
depois do commit do Extrato — ela quem parseia (OFX/CSV, issues #8/#9) e
grava os lançamentos, atualizando o status pra "processando"/"concluido"/
"concluido_com_erros"/"erro" (issue #13). O conteúdo continua nunca sendo
persistido (ADR-002): só existe em memória até a task terminar de
processar e descartar.

`GET /extratos/{extrato_id}` (issue #13) devolve o relatório de validação:
status atual, quantidade de lançamentos válidos e a lista de linhas/
transações que não normalizaram (identificador + motivo), pra quem chamou
o upload acompanhar o resultado do processamento assíncrono. Desde a issue
#82, também devolve `periodo_inicio`/`periodo_fim` (menor e maior data dos
lançamentos deste extrato, gravados por `app/services/normalizacao.py`),
sempre presentes na resposta, com `null` enquanto o extrato está
"pendente"/"processando" ou sem nenhum lançamento válido.

`GET /extratos` (issue #70) lista os extratos da empresa do token, inclusive
os enviados e ainda não conciliados, que `GET /execucoes` não alcança. Query:
`limit` (1 a 100, default 20), `offset` (>= 0), `origem` opcional e `status`
opcional e repetível (`?status=erro&status=concluido_com_erros` traz qualquer
um deles). `total` respeita os filtros. Ordem: `criado_em` desc, `id` desc
(mais recente primeiro, desempate estável pra paginar). Por item, além dos
metadados: `linhas_nao_lidas` (contagem de `linhas_invalidas`), o período
(`null` enquanto processa ou sem lançamento válido), `enviado_em` (o
`criado_em` do extrato, em UTC explícito como o `executada_em` de
app/api/execucoes.py) e `conciliado` (o extrato aparece em alguma execução
de conciliação, como banco OU como sistema). Duas consultas por página,
qualquer que seja o tamanho dela: a dos itens, com a contagem e o
`conciliado` em subconsultas correlacionadas, e a do total. Reenviar o mesmo
arquivo ainda cria outro extrato (pendência registrada, fora da #70).

Saldo (issue #118): `GET /extratos` (em cada item) e `GET /extratos/{id}`
devolvem `saldo_inicial`, `saldo_final` e `saldo_data`, sempre presentes,
`null` quando não há saldo (regras em app/services/saldo.py). Na listagem,
as somas dos lançamentos da página saem de uma terceira consulta, agrupada,
feita só quando algum item da página tem saldo e nenhuma linha não lida: o
número de consultas continua sem depender do tamanho da página.

Desde a issue #14, os endpoints exigem JWT (app/core/auth.py, ADR-003):
o `empresa_id` do Extrato é o da empresa ativa (cabeçalho `X-Ledgr-Empresa`,
conferido contra a conta do token desde a issue #133), nunca do form. `GET`
de extrato de outra empresa, inclusive de outra empresa da mesma conta,
devolve 404 (não 403) pra não confirmar a existência do ID (mitigação de
BOLA/IDOR). O upload tem rate limit de 10/min por conta.

Arquivo original (issue #139, ADR-023): nas contas do plano avançado, com
`ARQUIVO_ORIGINAL_HABILITADO` ligada, o upload cifra e envia o arquivo ao R2
antes do commit e grava a chave do objeto em `arquivo_chave`; nos outros
planos, o ADR-002 continua valendo. Uma falha no R2 não derruba o upload: o
extrato segue sem original. `arquivo_original` no upload, em cada item da
listagem e no detalhe diz se há original guardado (e a flag está ligada).
"""

import logging
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import EmpresaAtiva, obter_empresa_ativa, obter_empresa_id_ativa
from app.core.database import get_db
from app.core.rate_limit import LIMITE_UPLOAD, chave_por_conta, limiter
from app.models import ExecucaoConciliacao, Extrato, LinhaInvalida
from app.services import saldo
from app.services.arquivo_original import (
    ArquivoOriginal,
    chave_do_objeto,
    conta_guarda_o_original,
    obter_arquivo_original,
    original_visivel,
)
from app.services.normalizacao import normalizar_extrato

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extratos", tags=["extratos"])

FORMATOS_SUPORTADOS = {".ofx", ".csv"}
TAMANHO_MAXIMO_BYTES = 5 * 1024 * 1024  # 5MB — ver issue #7, sem ADR dedicado


class ExtratoUploadResponse(BaseModel):
    extrato_id: uuid.UUID
    status: str
    arquivo_original: bool


class ErroLinhaResponse(BaseModel):
    identificador: str
    motivo: str


class ExtratoDetalheResponse(BaseModel):
    extrato_id: uuid.UUID
    status: str
    origem: str
    quantidade_lancamentos: int | None
    periodo_inicio: date | None
    periodo_fim: date | None
    saldo_inicial: Decimal | None
    saldo_final: Decimal | None
    saldo_data: date | None
    erros: list[ErroLinhaResponse]
    arquivo_original: bool


@router.post(
    "/upload",
    response_model=ExtratoUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
)
@limiter.limit(LIMITE_UPLOAD, key_func=chave_por_conta)
async def upload_extrato(
    request: Request,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    arquivo: Annotated[UploadFile, File()],
    origem: Annotated[Literal["banco", "sistema"], Form()],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    servico: Annotated[ArquivoOriginal | None, Depends(obter_arquivo_original)],
) -> ExtratoUploadResponse:
    extensao = os.path.splitext(arquivo.filename or "")[1].lower()
    if extensao not in FORMATOS_SUPORTADOS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Formato de arquivo não suportado. Envie um arquivo .ofx ou .csv.",
        )

    conteudo = await arquivo.read()
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Arquivo excede o limite de 5MB.",
        )

    empresa = ativa.empresa
    extrato = Extrato(
        id=uuid.uuid4(),
        empresa_id=empresa.id,
        nome_arquivo=arquivo.filename,
        formato=extensao.lstrip("."),
        tamanho_bytes=len(conteudo),
        origem=origem,
        status="pendente",
    )
    # Plano avançado (ADR-023): o original vai cifrado para o R2 antes do
    # commit, e a chave só é gravada se o envio deu certo.
    if conta_guarda_o_original(empresa.conta, servico):
        extrato.arquivo_chave = await _guardar_original(
            servico, chave_do_objeto(empresa.conta_id, empresa.id, extrato.id), conteudo, extrato.id
        )

    db.add(extrato)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if extrato.arquivo_chave is not None:
            await _apagar_original_sem_extrato(servico, extrato.arquivo_chave, extrato.id)
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        ) from exc
    db.refresh(extrato)

    # Conteúdo nunca é gravado em disco nem em coluna (ADR-002) — só passa
    # em memória pra task de normalização, que descarta depois de processar.
    background_tasks.add_task(normalizar_extrato, extrato.id, extrato.formato, conteudo)

    return ExtratoUploadResponse(
        extrato_id=extrato.id,
        status=extrato.status,
        arquivo_original=original_visivel(extrato.arquivo_chave),
    )


async def _guardar_original(
    servico: ArquivoOriginal, chave: str, conteudo: bytes, extrato_id: uuid.UUID
) -> str | None:
    """Cifra e envia o original, fora do event loop (o cliente do R2 é
    síncrono). Qualquer falha deixa o extrato sem original, e o upload segue:
    o R2 fora do ar não impede a conciliação. No log, só a classe do erro."""
    try:
        await run_in_threadpool(servico.guardar, chave, conteudo)
    except Exception as exc:  # noqa: BLE001 - o upload segue sem o original (ADR-023)
        logger.warning(
            "arquivo original não guardado (%s), extrato %s", type(exc).__name__, extrato_id
        )
        return None
    return chave


async def _apagar_original_sem_extrato(
    servico: ArquivoOriginal, chave: str, extrato_id: uuid.UUID
) -> None:
    """O commit falhou depois do envio: tenta apagar o objeto, para não ficar
    arquivo no R2 sem extrato no banco."""
    try:
        await run_in_threadpool(servico.apagar, chave)
    except Exception as exc:  # noqa: BLE001 - o 404 do upload sai de qualquer jeito
        logger.warning(
            "arquivo original sem extrato não foi apagado (%s), extrato %s",
            type(exc).__name__,
            extrato_id,
        )


StatusExtrato = Literal["pendente", "processando", "concluido", "concluido_com_erros", "erro"]


class ItemExtratoResponse(BaseModel):
    extrato_id: uuid.UUID
    nome_arquivo: str
    origem: str
    formato: str
    status: str
    quantidade_lancamentos: int | None
    linhas_nao_lidas: int
    periodo_inicio: date | None
    periodo_fim: date | None
    saldo_inicial: Decimal | None
    saldo_final: Decimal | None
    saldo_data: date | None
    enviado_em: datetime
    conciliado: bool
    arquivo_original: bool


class ExtratoListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[ItemExtratoResponse]


@router.get("", response_model=ExtratoListaResponse)
def listar_extratos(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    origem: Annotated[Literal["banco", "sistema"] | None, Query()] = None,
    status: Annotated[list[StatusExtrato] | None, Query()] = None,
) -> ExtratoListaResponse:
    filtros = [Extrato.empresa_id == empresa_id]
    if origem is not None:
        filtros.append(Extrato.origem == origem)
    if status:
        filtros.append(Extrato.status.in_(sorted(set(status))))

    linhas_nao_lidas = (
        select(func.count())
        .where(LinhaInvalida.extrato_id == Extrato.id)
        .correlate(Extrato)
        .scalar_subquery()
    )
    conciliado = exists().where(
        ExecucaoConciliacao.empresa_id == empresa_id,
        or_(
            ExecucaoConciliacao.extrato_banco_id == Extrato.id,
            ExecucaoConciliacao.extrato_sistema_id == Extrato.id,
        ),
    )
    linhas = db.execute(
        select(
            Extrato.id,
            Extrato.nome_arquivo,
            Extrato.origem,
            Extrato.formato,
            Extrato.status,
            Extrato.quantidade_lancamentos,
            Extrato.periodo_inicio,
            Extrato.periodo_fim,
            Extrato.saldo_final,
            Extrato.saldo_data,
            Extrato.criado_em,
            Extrato.arquivo_chave,
            linhas_nao_lidas.label("linhas_nao_lidas"),
            conciliado.label("conciliado"),
        )
        .where(*filtros)
        .order_by(Extrato.criado_em.desc(), Extrato.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Extrato).where(*filtros))
    # Saldo inicial (issue #118): só soma os extratos que vão usar a soma.
    somas = saldo.somar_lancamentos_ate_saldo_data(
        db,
        empresa_id,
        [
            linha.id
            for linha in linhas
            if linha.saldo_final is not None and not linha.linhas_nao_lidas
        ],
    )

    itens = [
        ItemExtratoResponse(
            extrato_id=linha.id,
            nome_arquivo=linha.nome_arquivo,
            origem=linha.origem,
            formato=linha.formato,
            status=linha.status,
            quantidade_lancamentos=linha.quantidade_lancamentos,
            linhas_nao_lidas=linha.linhas_nao_lidas,
            periodo_inicio=linha.periodo_inicio,
            periodo_fim=linha.periodo_fim,
            **vars(
                saldo.montar_saldo(
                    linha.saldo_final,
                    linha.saldo_data,
                    linha.linhas_nao_lidas,
                    somas.get(linha.id, Decimal(0)),
                )
            ),
            enviado_em=linha.criado_em.replace(tzinfo=UTC),
            conciliado=linha.conciliado,
            arquivo_original=original_visivel(linha.arquivo_chave),
        )
        for linha in linhas
    ]
    return ExtratoListaResponse(total=total, limit=limit, offset=offset, itens=itens)


@router.get("/{extrato_id}", response_model=ExtratoDetalheResponse)
async def obter_extrato(
    extrato_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> ExtratoDetalheResponse:
    extrato = db.get(Extrato, extrato_id)
    if extrato is None or extrato.empresa_id != empresa_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Extrato não encontrado.",
        )

    erros = [
        ErroLinhaResponse(identificador=linha.identificador, motivo=linha.motivo)
        for linha in extrato.linhas_invalidas
    ]

    return ExtratoDetalheResponse(
        extrato_id=extrato.id,
        status=extrato.status,
        origem=extrato.origem,
        quantidade_lancamentos=extrato.quantidade_lancamentos,
        periodo_inicio=extrato.periodo_inicio,
        periodo_fim=extrato.periodo_fim,
        **vars(saldo.saldo_do_extrato(db, extrato)),
        erros=erros,
        arquivo_original=original_visivel(extrato.arquivo_chave),
    )
