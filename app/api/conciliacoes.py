"""Endpoints de conciliação: `POST /conciliacoes` (issue #16) roda o motor de
matching exato (app/services/matching.py, ADR-006) pro par de extratos e grava
o resultado em `conciliacoes` (ADR-007). `GET /conciliacoes/{extrato_id}`
(issue #20) consulta o que foi gravado.

É síncrono de propósito (sem BackgroundTasks): o motor exato é só agrupamento
em memória e um insert em lote, e a resposta já traz as contagens.

Exige JWT (app/core/auth.py, ADR-003): o `empresa_id` vem do token. Extrato
inexistente ou de outra empresa devolve 404 igual, sem confirmar a existência
do ID (mitigação de BOLA/IDOR, mesmo padrão de app/api/extratos.py).

Validações, nesta ordem: (a) os dois extratos existem e são da empresa do
token (404); (b) ids diferentes (422); (c) o primeiro tem origem "banco" e o
segundo "sistema" (422); (d) ambos com status "concluido" ou
"concluido_com_erros" (409).

`GET /conciliacoes/{extrato_id}`: o `extrato_id` é o extrato do BANCO, porque a
consulta parte de `extrato_banco_id`, que tem índice (ADR-007). Um extrato do
sistema pode ter sido conciliado com vários extratos de banco, então consultar
por ele seria ambíguo (422). Validações: (a) extrato existe e é da empresa do
token (404, sem confirmar a existência de extrato de outra empresa); (b) origem
diferente de "sistema" (422). Extrato do banco sem nenhuma conciliação devolve
200 com lista vazia. Query params opcionais: `limit` (1 a 1000, default 100),
`offset`, `status` e `extrato_sistema_id` (filtra um par específico). Os itens
saem numa única query com outer join nos dois lançamentos, sempre filtrando por
`empresa_id`, e a ordem é estável pra paginar: data do lançamento (a do banco,
ou a do sistema quando não há do banco), valor e id da conciliação. `valor` e
`score_confianca` são Decimal e vão como string no JSON, pra o cliente não
tratar dinheiro como float.

`GET /conciliacoes/{extrato_id}/exportar` (issue #26): exporta em CSV (Excel
BR — formato em app/services/exportacao.py) o mesmo recorte da listagem
acima, sem paginação (exporta todas as linhas do filtro). Reusa a validação
do extrato (`_validar_extrato_banco`) e a montagem da query
(`_construir_consulta_itens`) da listagem — mesmas validações, mesmos
códigos, mesmos filtros e mesma ordenação; o que muda é que a exportação não
pagina e devolve CSV em vez de JSON.

Decisões por linha (issue #83, regras em app/services/decisoes.py): cada
item da listagem traz `chave` (identifica a linha em todas as rodadas),
`decisao` (a decisão em vigor ou `null`, SEMPRE presente — o front liga a
caixa de conferir e o "Justificar" pela presença dela) e `eventos` (o
histórico completo da chave, em ordem cronológica, `[]` quando não há).
Os eventos da página saem de UMA consulta, qualquer que seja o tamanho da
página. A decisão é por `(empresa_id, extrato_banco_id, chave)`, então vale
para a mesma linha em qualquer rodada; sem `extrato_sistema_id` na query e
com várias rodadas, a mesma chave aparece uma vez por par, com a mesma
decisão (o front sempre filtra por par). A exportação CSV não traz decisões.

`POST /conciliacoes/{extrato_id}/decisoes` grava um evento (conferir,
justificar ou desfazer) na rodada mais recente do extrato do banco e devolve
a decisão em vigor depois dele. Autentica o usuário (`sub` do token), que vira
o autor. Pega a trava do extrato do banco (a mesma de `conciliar_extratos`,
app/services/travas.py) ANTES de calcular a rodada mais recente, para não
decidir sobre uma linha enquanto uma rodada nova a substitui, mesmo com outro
extrato do sistema.
Erros de regra saem como 422 com `detail` em texto, porque o front só mostra
o motivo quando `detail` é string. Nunca loga o texto da justificativa nem a
descrição de lançamento.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import obter_empresa_id_autenticada, obter_usuario_autenticado
from app.core.database import get_db
from app.models import Conciliacao, DecisaoLinha, Extrato, Lancamento, Usuario
from app.services import decisoes
from app.services.exportacao import LinhaExportacao, gerar_csv_conciliacoes
from app.services.matching import conciliar_extratos
from app.services.travas import travar_extrato_banco

router = APIRouter(prefix="/conciliacoes", tags=["conciliacoes"])

STATUS_PROCESSADO = {"concluido", "concluido_com_erros"}

StatusConciliacao = Literal[
    "match_exato",
    "match_tolerancia",
    "divergente_valor",
    "divergente_data",
    "sem_correspondencia",
    "duplicado",
    "tarifa_bancaria",
]


class ConciliacaoRequest(BaseModel):
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID


class ConciliacaoResponse(BaseModel):
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    total: int
    match_exato: int
    match_tolerancia: int
    duplicado: int
    sem_correspondencia: int
    tarifa_bancaria: int
    divergente_valor: int
    divergente_data: int


def _obter_extrato_da_empresa(db: Session, extrato_id: uuid.UUID, empresa_id: uuid.UUID) -> Extrato:
    extrato = db.get(Extrato, extrato_id)
    if extrato is None or extrato.empresa_id != empresa_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Extrato não encontrado.",
        )
    return extrato


@router.post(
    "",
    response_model=ConciliacaoResponse,
    status_code=http_status.HTTP_201_CREATED,
)
def criar_conciliacao(
    corpo: ConciliacaoRequest,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
) -> ConciliacaoResponse:
    extrato_banco = _obter_extrato_da_empresa(db, corpo.extrato_banco_id, empresa_id)
    extrato_sistema = _obter_extrato_da_empresa(db, corpo.extrato_sistema_id, empresa_id)

    if corpo.extrato_banco_id == corpo.extrato_sistema_id:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Os extratos do banco e do sistema devem ser diferentes.",
        )

    if extrato_banco.origem != "banco" or extrato_sistema.origem != "sistema":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="O primeiro extrato deve ter origem 'banco' e o segundo, origem 'sistema'.",
        )

    if (
        extrato_banco.status not in STATUS_PROCESSADO
        or extrato_sistema.status not in STATUS_PROCESSADO
    ):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Um dos extratos ainda não terminou de processar ou falhou.",
        )

    contagens = conciliar_extratos(db, empresa_id, corpo.extrato_banco_id, corpo.extrato_sistema_id)
    return ConciliacaoResponse(
        extrato_banco_id=corpo.extrato_banco_id,
        extrato_sistema_id=corpo.extrato_sistema_id,
        **contagens,
    )


class LancamentoResponse(BaseModel):
    id: uuid.UUID
    data: date
    valor: Decimal
    descricao: str
    tipo: str


class DecisaoResponse(BaseModel):
    tipo: str
    texto: str | None
    autor: str
    em: datetime
    rodada: int


def _decisao_response(evento: DecisaoLinha | None) -> DecisaoResponse | None:
    if evento is None:
        return None
    return DecisaoResponse(**vars(decisoes.como_evento(evento)))


class ItemConciliacaoResponse(BaseModel):
    id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    status: str
    regra_aplicada: str | None
    score_confianca: Decimal | None
    lancamento_banco: LancamentoResponse | None
    lancamento_sistema: LancamentoResponse | None
    chave: str
    decisao: DecisaoResponse | None
    eventos: list[DecisaoResponse]


class ConciliacaoListaResponse(BaseModel):
    extrato_id: uuid.UUID
    total: int
    limit: int
    offset: int
    itens: list[ItemConciliacaoResponse]


def _lancamento_ou_none(linha, prefixo: str) -> LancamentoResponse | None:
    id_ = getattr(linha, f"{prefixo}_id")
    if id_ is None:
        return None
    return LancamentoResponse(
        id=id_,
        data=getattr(linha, f"{prefixo}_data"),
        valor=getattr(linha, f"{prefixo}_valor"),
        descricao=getattr(linha, f"{prefixo}_descricao"),
        tipo=getattr(linha, f"{prefixo}_tipo"),
    )


def _chave(linha) -> str:
    return decisoes.chave_da_linha(
        linha.banco_id,
        linha.sistema_valor,
        linha.sistema_data,
        linha.sistema_descricao,
        linha.sistema_ocorrencia,
    )


def _validar_extrato_banco(db: Session, extrato_id: uuid.UUID, empresa_id: uuid.UUID) -> Extrato:
    """Extrato do banco validado (issue #20, reusado pela exportação da
    issue #26): existe, é da empresa do token (404 igual pros dois casos,
    sem confirmar a existência do ID de outra empresa) e tem origem
    diferente de "sistema" (422)."""
    extrato = _obter_extrato_da_empresa(db, extrato_id, empresa_id)
    if extrato.origem == "sistema":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A consulta deve usar o extrato do banco, não o do sistema.",
        )
    return extrato


def _construir_consulta_itens(
    empresa_id: uuid.UUID,
    extrato_id: uuid.UUID,
    status: StatusConciliacao | None,
    extrato_sistema_id: uuid.UUID | None,
) -> tuple[Select, list]:
    """Query compartilhada entre a listagem (issue #20) e a exportação CSV
    (issue #26): mesmos filtros (sempre por `empresa_id` e
    `extrato_banco_id`, ADR-004), os mesmos dois outer joins nos lançamentos
    e a mesma ordenação estável (data do lançamento, valor, id da
    conciliação) — sem `limit`/`offset`, que cada endpoint aplica (ou não)
    do seu jeito. Devolve a `Select` e a lista de filtros, essa última
    reusada pelo `count()` da listagem."""
    filtros = [
        Conciliacao.empresa_id == empresa_id,
        Conciliacao.extrato_banco_id == extrato_id,
    ]
    if status is not None:
        filtros.append(Conciliacao.status == status)
    if extrato_sistema_id is not None:
        filtros.append(Conciliacao.extrato_sistema_id == extrato_sistema_id)

    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    consulta = (
        select(
            Conciliacao.id,
            Conciliacao.extrato_sistema_id,
            Conciliacao.status,
            Conciliacao.regra_aplicada,
            Conciliacao.score_confianca,
            lanc_banco.id.label("banco_id"),
            lanc_banco.data.label("banco_data"),
            lanc_banco.valor.label("banco_valor"),
            lanc_banco.descricao.label("banco_descricao"),
            lanc_banco.tipo.label("banco_tipo"),
            lanc_sistema.id.label("sistema_id"),
            lanc_sistema.data.label("sistema_data"),
            lanc_sistema.valor.label("sistema_valor"),
            lanc_sistema.descricao.label("sistema_descricao"),
            lanc_sistema.tipo.label("sistema_tipo"),
            lanc_sistema.ocorrencia.label("sistema_ocorrencia"),
        )
        .select_from(Conciliacao)
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(*filtros)
        .order_by(
            func.coalesce(lanc_banco.data, lanc_sistema.data),
            func.coalesce(lanc_banco.valor, lanc_sistema.valor),
            Conciliacao.id,
        )
    )
    return consulta, filtros


@router.get("/{extrato_id}", response_model=ConciliacaoListaResponse)
def listar_conciliacoes(
    extrato_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: StatusConciliacao | None = None,
    extrato_sistema_id: uuid.UUID | None = None,
) -> ConciliacaoListaResponse:
    _validar_extrato_banco(db, extrato_id, empresa_id)
    consulta, filtros = _construir_consulta_itens(
        empresa_id, extrato_id, status, extrato_sistema_id
    )
    linhas = db.execute(consulta.limit(limit).offset(offset)).all()
    total = db.scalar(select(func.count()).select_from(Conciliacao).where(*filtros))

    chaves = [_chave(linha) for linha in linhas]
    eventos_por_chave = decisoes.carregar_eventos(db, empresa_id, extrato_id, set(chaves))

    itens = []
    for linha, chave in zip(linhas, chaves, strict=True):
        eventos = eventos_por_chave.get(chave, [])
        itens.append(
            ItemConciliacaoResponse(
                id=linha.id,
                extrato_sistema_id=linha.extrato_sistema_id,
                status=linha.status,
                regra_aplicada=linha.regra_aplicada,
                score_confianca=linha.score_confianca,
                lancamento_banco=_lancamento_ou_none(linha, "banco"),
                lancamento_sistema=_lancamento_ou_none(linha, "sistema"),
                chave=chave,
                decisao=_decisao_response(decisoes.decisao_vigente(eventos)),
                eventos=[_decisao_response(evento) for evento in eventos],
            )
        )
    return ConciliacaoListaResponse(
        extrato_id=extrato_id, total=total, limit=limit, offset=offset, itens=itens
    )


@router.get("/{extrato_id}/exportar")
def exportar_conciliacoes(
    extrato_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    status: StatusConciliacao | None = None,
    extrato_sistema_id: uuid.UUID | None = None,
) -> Response:
    """Exportação CSV do relatório de conciliação (issue #26): mesmo recorte
    de `listar_conciliacoes` (mesma validação, mesmos filtros, mesma
    ordenação — ver `_validar_extrato_banco`/`_construir_consulta_itens`),
    sem paginação. Formatação e neutralização de injeção de fórmula em
    app/services/exportacao.py."""
    _validar_extrato_banco(db, extrato_id, empresa_id)
    consulta, _ = _construir_consulta_itens(empresa_id, extrato_id, status, extrato_sistema_id)
    linhas = db.execute(consulta).all()

    linhas_exportacao = [
        LinhaExportacao(
            status=linha.status,
            regra_aplicada=linha.regra_aplicada,
            score_confianca=linha.score_confianca,
            banco_data=linha.banco_data,
            banco_valor=linha.banco_valor,
            banco_descricao=linha.banco_descricao,
            sistema_data=linha.sistema_data,
            sistema_valor=linha.sistema_valor,
            sistema_descricao=linha.sistema_descricao,
        )
        for linha in linhas
    ]
    conteudo = gerar_csv_conciliacoes(linhas_exportacao)

    return Response(
        content=conteudo,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="conciliacao-{str(extrato_id)[:8]}.csv"',
            "Cache-Control": "no-store",
        },
    )


TAMANHO_MAXIMO_JUSTIFICATIVA = 1000

TipoDecisao = Literal["conferida", "conferencia_desfeita", "justificada", "justificativa_desfeita"]


class DecisaoRequest(BaseModel):
    chave: str
    tipo: TipoDecisao
    texto: str | None = None


def _regra_violada(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detalhe)


def _linha_da_chave(
    db: Session,
    empresa_id: uuid.UUID,
    extrato_banco_id: uuid.UUID,
    extrato_sistema_id: uuid.UUID,
    chave: str,
):
    """A linha do par com essa chave, ou `None`. Chave em formato de UUID só
    pode ser de linha com lançamento do banco, então filtra por ele; senão,
    calcula a chave das linhas só do sistema do par."""
    consulta, _ = _construir_consulta_itens(empresa_id, extrato_banco_id, None, extrato_sistema_id)
    try:
        lancamento_banco_id = uuid.UUID(chave)
    except ValueError:
        consulta = consulta.where(Conciliacao.lancamento_banco_id.is_(None))
    else:
        if str(lancamento_banco_id) != chave:
            return None
        consulta = consulta.where(Conciliacao.lancamento_banco_id == lancamento_banco_id)
    return next((linha for linha in db.execute(consulta) if _chave(linha) == chave), None)


@router.post("/{extrato_id}/decisoes", response_model=DecisaoResponse | None)
def registrar_decisao(
    extrato_id: uuid.UUID,
    corpo: DecisaoRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> DecisaoResponse | None:
    empresa_id = usuario.empresa_id
    _validar_extrato_banco(db, extrato_id, empresa_id)

    # A trava vem ANTES da rodada: uma rodada nova com outro extrato do
    # sistema espera esta decisão terminar, ou esta decisão espera a rodada.
    travar_extrato_banco(db, empresa_id, extrato_id)

    rodada = decisoes.rodada_mais_recente(db, empresa_id, extrato_id)
    if rodada is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Este extrato ainda não foi conciliado.",
        )

    linha = _linha_da_chave(db, empresa_id, extrato_id, rodada.extrato_sistema_id, corpo.chave)
    if linha is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Esta linha não está mais nesta conciliação.",
        )
    if not decisoes.diverge(linha.status):
        raise _regra_violada("Só é possível decidir sobre uma linha que diverge.")

    texto = None
    if corpo.tipo == "justificada":
        texto = (corpo.texto or "").strip()
        if not texto:
            raise _regra_violada("A justificativa precisa de um texto.")
        if len(texto) > TAMANHO_MAXIMO_JUSTIFICATIVA:
            raise _regra_violada(
                f"A justificativa pode ter até {TAMANHO_MAXIMO_JUSTIFICATIVA} caracteres."
            )
    elif corpo.tipo in decisoes.TIPOS_DESFAZER:
        eventos = decisoes.carregar_eventos(db, empresa_id, extrato_id, {corpo.chave})
        vigente = decisoes.decisao_vigente(eventos.get(corpo.chave, []))
        if vigente is None or vigente.tipo != decisoes.TIPOS_DESFAZER[corpo.tipo]:
            if corpo.tipo == "conferencia_desfeita":
                raise _regra_violada("Não há conferência para desfazer nesta linha.")
            raise _regra_violada("Não há justificativa para desfazer nesta linha.")

    evento = DecisaoLinha(
        empresa_id=empresa_id,
        extrato_banco_id=extrato_id,
        chave=corpo.chave,
        tipo=corpo.tipo,
        texto=texto,
        usuario_id=usuario.id,
        autor_nome=usuario.nome,
        rodada=rodada.numero,
        extrato_sistema_id=rodada.extrato_sistema_id,
    )
    db.add(evento)
    db.commit()

    if corpo.tipo in decisoes.TIPOS_DESFAZER:
        return None
    db.refresh(evento)
    return _decisao_response(evento)
