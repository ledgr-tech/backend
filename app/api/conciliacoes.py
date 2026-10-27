"""Endpoints de conciliação: `POST /conciliacoes` (issue #16) roda o motor de
matching exato (app/services/matching.py, ADR-006) pro par de extratos e grava
o resultado em `conciliacoes` (ADR-007). `GET /conciliacoes/{extrato_id}`
(issue #20) consulta o que foi gravado.

É síncrono de propósito (sem BackgroundTasks): o motor exato é só agrupamento
em memória e um insert em lote, e a resposta já traz as contagens.

Exige JWT (app/core/auth.py, ADR-003): o `empresa_id` é o da empresa ativa
(cabeçalho `X-Ledgr-Empresa`, conferido contra a conta do token desde a issue
#133). Extrato inexistente ou de outra empresa, inclusive de outra empresa da
mesma conta, devolve 404 igual, sem confirmar a existência
do ID (mitigação de BOLA/IDOR, mesmo padrão de app/api/extratos.py).

Validações, nesta ordem: (a) os dois extratos existem e são da empresa do
token (404); (b) ids diferentes (422); (c) o primeiro tem origem "banco" e o
segundo "sistema" (422); (d) ambos com status "concluido" ou
"concluido_com_erros" (409); (e) a competência do extrato do banco (mês de
`periodo_inicio`) não está fechada (409, issue #86). A checagem (e) pega a
trava do mês (compartilhada) e a do extrato do banco, nessa ordem
(app/services/travas.py); extrato sem período não tem competência e segue
liberado.

Explicação automática (issue #140, ADR-011): com
`EXPLICACAO_AUTOMATICA_HABILITADA` e `LLM_HABILITADO` ligadas, o POST agenda
em `BackgroundTasks`, depois do commit e da resposta, a explicação das
divergências do par (app/services/ia/geracao.py). A resposta e o tempo do
POST não mudam: a IA não fica no caminho da requisição.

Plano (issue #122): com `PLANO_BLOQUEIO_HABILITADO`, entre (d) e (e), antes de
qualquer trava ou processamento, a primeira conciliação de um extrato do banco
(nenhuma execução dele ainda) dá 402 com `detail` em texto quando a empresa
está "sem_plano" ou "teste_encerrado" (app/services/plano.py). Reconciliar um
extrato do banco que já tem execução segue liberado.

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
tratar dinheiro como float. Na raiz, `saldo_inicial`, `saldo_final` e
`saldo_data` do extrato do banco (issue #118, regras em
app/services/saldo.py), `null` quando ele não tem saldo; os saldos também são
Decimal em string.

`GET /conciliacoes/{extrato_id}/exportar` (issue #26): exporta em CSV (Excel
BR — formato em app/services/exportacao.py) o mesmo recorte da listagem
acima, sem paginação (exporta todas as linhas do filtro). Reusa a validação
do extrato (`_validar_extrato_banco`) e a montagem da query
(`_construir_consulta_itens`) da listagem — mesmas validações, mesmos
códigos, mesmos filtros e mesma ordenação; o que muda é que a exportação não
pagina e devolve CSV em vez de JSON.

Explicação (issue #140): cada item traz `explicacao`, o texto da IA ligado à
linha (`conciliacoes.explicacao_chave`, pela geração automática ou pelo
clique no `POST /explicacoes`), e `gerada_por_ia`, `true` só quando há esse
texto. Sem texto da IA (geração desligada, teto, limite, falha ou ainda não
gerado), `explicacao` é `null` e `gerada_por_ia` é `false`; o texto fixo por
categoria não sai aqui, o front tem o dele. Com `LLM_HABILITADO` desligado,
`null` em todos os itens, mesmo com cache (o interruptor de emergência do
ADR-011). O texto vem num join com o cache, pela empresa da linha: nenhuma
consulta por item, e a explicação de uma empresa nunca aparece em outra.

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

Aprovação (issue #132, ADR-019): toda justificativa nasce aguardando
aprovação. `decisao` traz `situacao` ("pendente" ou "aprovada" na
justificada, `null` na conferida) e, quando aprovada, `aprovada_por` e
`aprovada_em`. Os eventos de aprovação e de rejeição aparecem em `eventos`,
com o motivo da rejeição em `texto`. Na raiz, `aguardando_aprovacao` conta as
linhas divergentes da rodada atual com justificativa aguardando aprovação.

`POST /conciliacoes/{extrato_id}/decisoes` grava um evento (conferir,
justificar, desfazer, aprovar ou rejeitar a justificativa) na rodada mais
recente do extrato do banco e devolve a decisão em vigor depois dele (`null`
depois de desfazer ou rejeitar). Aprovar e rejeitar são só do admin e do
gestor (403 para o analista, antes de qualquer outra validação) e exigem uma
justificativa aguardando aprovação na linha (422). A rejeição aceita um
motivo opcional em `texto`, com o tamanho máximo da justificativa; a
aprovação não tem texto. Quem justificou pode aprovar a própria.

Desfazer a aprovação (issue #164, `aprovacao_desfeita`, sem texto) volta a
justificativa aprovada para aguardando aprovação e devolve a decisão em vigor,
pendente. A aprovação e a reversão ficam em `eventos`. Só quem aprovou desfaz
(403 para qualquer outro, inclusive o admin), com três exceções do admin:
quem aprovou foi removido, saiu da conta ou não é mais admin nem gestor, ou a
aprovação é da migração da #132 (`AUTOR_APROVACAO_DA_MIGRACAO`). O analista
recebe 403 antes de qualquer outra validação, como em aprovar. Com
`APROVACAO_JUSTIFICATIVAS_HABILITADA` desligada, 422, o código de aprovar com
a flag desligada (lá, sem justificativa pendente): sem a flag não existe o
estado pendente. Linha sem justificativa aprovada em vigor (pendente,
rejeitada, conferida ou sem decisão) também é 422, como aprovar sem
justificativa pendente. Mês fechado é 409, com a mesma mensagem das outras
decisões.

Com a flag ligada, a justificativa aprovada em vigor não pode ser alterada
nem retirada (`TIPOS_QUE_TIRAM_A_APROVACAO`: justificar de novo, conferir e
`justificativa_desfeita`): 409 para qualquer papel, até quem aprovou desfazer
a aprovação. Depois disso, pendente, as três seguem as regras de antes. Com a
flag desligada, nada muda.

Enquanto `APROVACAO_JUSTIFICATIVAS_HABILITADA` estiver desligada (o padrão, até
o front aprovar), a justificativa é gravada já aprovada: na mesma transação
entra a `justificativa_aprovada` em nome de quem justificou, um microssegundo
depois dela. Nada fica aguardando, e o fechamento funciona como antes. As
rotas de aprovar e rejeitar, e o 403 do analista nelas, valem nos dois
estados. Autentica o usuário (`sub` do token), que vira
o autor. Pega a trava do extrato do banco (a mesma de `conciliar_extratos`,
app/services/travas.py) ANTES de calcular a rodada mais recente, para não
decidir sobre uma linha enquanto uma rodada nova a substitui, mesmo com outro
extrato do sistema.
Com a competência do extrato do banco fechada, dá 409 antes de calcular a
rodada (issue #86). Erros de regra saem como 422 com `detail` em texto, porque o front só mostra
o motivo quando `detail` é string. Nunca loga o texto da justificativa nem a
descrição de lançamento.
"""

import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, aliased

from app.core.auth import (
    EmpresaAtiva,
    mensagem_de_papel,
    obter_empresa_ativa,
    obter_empresa_id_ativa,
)
from app.core.config import settings
from app.core.database import get_db
from app.models import (
    Conciliacao,
    DecisaoLinha,
    ExplicacaoDivergencia,
    Extrato,
    Lancamento,
    Usuario,
)
from app.services import decisoes, saldo
from app.services.exportacao import LinhaExportacao, gerar_csv_conciliacoes
from app.services.fechamentos import MesFechadoError, garantir_mes_aberto
from app.services.ia import ProvedorDeIA
from app.services.ia.geracao import (
    automatica_ligada,
    gerar_explicacoes_do_par,
    obter_provedor_dependencia,
)
from app.services.matching import conciliar_extratos
from app.services.plano import motivo_do_bloqueio

logger = logging.getLogger(__name__)

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


def _garantir_mes_aberto(db: Session, empresa_id: uuid.UUID, extrato_banco: Extrato) -> None:
    """Travas do mês (compartilhada) e do extrato do banco, e 409 se a
    competência do extrato do banco estiver fechada (issue #86)."""
    try:
        garantir_mes_aberto(db, empresa_id, extrato_banco)
    except MesFechadoError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"A competência {exc.competencia} está fechada. Reabra o fechamento para alterar."
            ),
        ) from exc


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
    tarefas: BackgroundTasks,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeIA | None, Depends(obter_provedor_dependencia)],
) -> ConciliacaoResponse:
    empresa_id = ativa.empresa.id
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

    motivo = motivo_do_bloqueio(db, ativa.empresa, corpo.extrato_banco_id)
    if motivo is not None:
        logger.info("conciliacao resultado=bloqueada_pelo_plano")
        raise HTTPException(status_code=http_status.HTTP_402_PAYMENT_REQUIRED, detail=motivo)

    _garantir_mes_aberto(db, empresa_id, extrato_banco)
    contagens = conciliar_extratos(db, empresa_id, corpo.extrato_banco_id, corpo.extrato_sistema_id)
    if provedor is not None and automatica_ligada():
        # Depois do commit de `conciliar_extratos` e da resposta.
        tarefas.add_task(
            gerar_explicacoes_do_par,
            empresa_id,
            corpo.extrato_banco_id,
            corpo.extrato_sistema_id,
            provedor,
        )
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
    # Só em `decisao` (issue #132); nos itens de `eventos`, sempre `null`.
    situacao: decisoes.SituacaoJustificativa | None = None
    aprovada_por: str | None = None
    aprovada_em: datetime | None = None


def _evento_response(evento: DecisaoLinha) -> DecisaoResponse:
    return DecisaoResponse(**vars(decisoes.como_evento(evento)))


def _decisao_response(vigente: decisoes.DecisaoEmVigor | None) -> DecisaoResponse | None:
    if vigente is None:
        return None
    aprovacao = vigente.aprovacao
    return DecisaoResponse(
        **vars(decisoes.como_evento(vigente.evento)),
        situacao=vigente.situacao,
        aprovada_por=aprovacao.autor_nome if aprovacao is not None else None,
        aprovada_em=aprovacao.em if aprovacao is not None else None,
    )


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
    explicacao: str | None
    gerada_por_ia: bool


class ConciliacaoListaResponse(BaseModel):
    extrato_id: uuid.UUID
    saldo_inicial: Decimal | None
    saldo_final: Decimal | None
    saldo_data: date | None
    total: int
    limit: int
    offset: int
    aguardando_aprovacao: int
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
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: StatusConciliacao | None = None,
    extrato_sistema_id: uuid.UUID | None = None,
) -> ConciliacaoListaResponse:
    extrato_banco = _validar_extrato_banco(db, extrato_id, empresa_id)
    consulta, filtros = _construir_consulta_itens(
        empresa_id, extrato_id, status, extrato_sistema_id
    )
    if settings.llm_habilitado:
        # O texto da IA ligado à linha, no mesmo SELECT e pela empresa dela.
        consulta = consulta.add_columns(
            ExplicacaoDivergencia.texto.label("explicacao_texto")
        ).outerjoin(
            ExplicacaoDivergencia,
            (ExplicacaoDivergencia.empresa_id == Conciliacao.empresa_id)
            & (ExplicacaoDivergencia.chave == Conciliacao.explicacao_chave),
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
                eventos=[_evento_response(evento) for evento in eventos],
                explicacao=getattr(linha, "explicacao_texto", None),
                gerada_por_ia=getattr(linha, "explicacao_texto", None) is not None,
            )
        )
    return ConciliacaoListaResponse(
        extrato_id=extrato_id,
        **vars(saldo.saldo_do_extrato(db, extrato_banco)),
        total=total,
        limit=limit,
        offset=offset,
        aguardando_aprovacao=_aguardando_na_rodada_atual(db, empresa_id, extrato_id),
        itens=itens,
    )


def _aguardando_na_rodada_atual(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID
) -> int:
    """Linhas divergentes da rodada mais recente com justificativa aguardando
    aprovação; 0 sem conciliação. Três consultas, qualquer que seja a página."""
    rodada = decisoes.rodada_mais_recente(db, empresa_id, extrato_banco_id)
    if rodada is None:
        return 0
    par = (extrato_banco_id, rodada.extrato_sistema_id)
    contagem = decisoes.contar_justificativas(db, empresa_id, {par}).get(par)
    return contagem.aguardando_aprovacao if contagem is not None else 0


@router.get("/{extrato_id}/exportar")
def exportar_conciliacoes(
    extrato_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
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

TipoDecisao = Literal[
    "conferida",
    "conferencia_desfeita",
    "justificada",
    "justificativa_desfeita",
    "justificativa_aprovada",
    "justificativa_rejeitada",
    "aprovacao_desfeita",
]

SEM_JUSTIFICATIVA_AGUARDANDO = "Não há justificativa aguardando aprovação nesta linha."
# Quem aprova e rejeita justificativas (issue #132, ADR-019).
PAPEIS_QUE_APROVAM = ("admin", "gestor")
# Desfazer a aprovação (issue #164).
APROVACAO_DESLIGADA = (
    "A aprovação de justificativas está desligada, então não há aprovação para desfazer."
)
SEM_APROVACAO_PARA_DESFAZER = "Não há aprovação para desfazer nesta linha."
SO_QUEM_APROVOU = "Só quem aprovou pode desfazer a aprovação."
JUSTIFICATIVA_APROVADA = (
    "Esta justificativa está aprovada. Quem aprovou precisa desfazer a aprovação "
    "antes de alterá-la ou retirá-la."
)
# Os tipos que tiram de vigor uma justificativa aprovada (issue #164):
# justificar de novo e conferir a substituem, e a retirada a desfaz.
TIPOS_QUE_TIRAM_A_APROVACAO = ("justificada", "conferida", "justificativa_desfeita")


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


def _pode_desfazer_aprovacao(db: Session, usuario: Usuario, aprovacao: DecisaoLinha) -> bool:
    """Quem aprovou desfaz. O admin desfaz também quando quem aprovou não pode
    mais (removido, de outra conta ou sem papel de aprovar) e na aprovação da
    migração da #132, que não é de ninguém: sem isso, essas aprovações ficariam
    impossíveis de desfazer. O analista já recebeu 403 antes."""
    da_migracao = aprovacao.autor_nome == decisoes.AUTOR_APROVACAO_DA_MIGRACAO
    if not da_migracao and aprovacao.usuario_id == usuario.id:
        return True
    if usuario.papel != "admin":
        return False
    if da_migracao:
        return True
    aprovador = db.get(Usuario, aprovacao.usuario_id)
    return (
        aprovador is None
        or aprovador.removido_em is not None
        or aprovador.conta_id != usuario.conta_id
        or aprovador.papel not in PAPEIS_QUE_APROVAM
    )


def _aprovar_junto(db: Session, justificativa: DecisaoLinha) -> None:
    """Aprovação de quem justificou, na mesma transação, com a flag de
    aprovação desligada. O `em` dela é o da justificativa mais um
    microssegundo: a ordem dos eventos decide a decisão em vigor, e dois
    INSERTs seguidos podem cair no mesmo instante do `clock_timestamp()`."""
    db.flush()
    db.refresh(justificativa, ["em"])
    db.add(
        DecisaoLinha(
            empresa_id=justificativa.empresa_id,
            extrato_banco_id=justificativa.extrato_banco_id,
            chave=justificativa.chave,
            tipo="justificativa_aprovada",
            texto=None,
            usuario_id=justificativa.usuario_id,
            autor_nome=justificativa.autor_nome,
            rodada=justificativa.rodada,
            extrato_sistema_id=justificativa.extrato_sistema_id,
            em=justificativa.em + timedelta(microseconds=1),
        )
    )


@router.post("/{extrato_id}/decisoes", response_model=DecisaoResponse | None)
def registrar_decisao(
    extrato_id: uuid.UUID,
    corpo: DecisaoRequest,
    ativa: Annotated[EmpresaAtiva, Depends(obter_empresa_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> DecisaoResponse | None:
    usuario, empresa_id = ativa.usuario, ativa.empresa.id
    desfaz_aprovacao = corpo.tipo == decisoes.TIPO_APROVACAO_DESFEITA
    if (
        corpo.tipo in decisoes.TIPOS_APROVACAO or desfaz_aprovacao
    ) and usuario.papel not in PAPEIS_QUE_APROVAM:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=mensagem_de_papel(PAPEIS_QUE_APROVAM),
        )
    if desfaz_aprovacao and not settings.aprovacao_justificativas_habilitada:
        raise _regra_violada(APROVACAO_DESLIGADA)
    extrato_banco = _validar_extrato_banco(db, extrato_id, empresa_id)

    # As travas (mês e extrato do banco) vêm ANTES da rodada: uma rodada nova
    # com outro extrato do sistema espera esta decisão terminar, ou esta
    # decisão espera a rodada; e um fechamento em andamento é esperado.
    _garantir_mes_aberto(db, empresa_id, extrato_banco)

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
    elif corpo.tipo in decisoes.TIPOS_APROVACAO:
        eventos = decisoes.carregar_eventos(db, empresa_id, extrato_id, {corpo.chave})
        if not decisoes.aguardando_aprovacao(
            decisoes.decisao_vigente(eventos.get(corpo.chave, []))
        ):
            raise _regra_violada(SEM_JUSTIFICATIVA_AGUARDANDO)
        motivo = (corpo.texto or "").strip() or None
        if corpo.tipo == "justificativa_aprovada" and motivo is not None:
            raise _regra_violada("A aprovação não tem texto.")
        if motivo is not None and len(motivo) > TAMANHO_MAXIMO_JUSTIFICATIVA:
            raise _regra_violada(
                f"O motivo pode ter até {TAMANHO_MAXIMO_JUSTIFICATIVA} caracteres."
            )
        texto = motivo
    elif desfaz_aprovacao:
        if (corpo.texto or "").strip():
            raise _regra_violada("Desfazer a aprovação não tem texto.")
        eventos = decisoes.carregar_eventos(db, empresa_id, extrato_id, {corpo.chave})
        vigente = decisoes.decisao_vigente(eventos.get(corpo.chave, []))
        if not decisoes.justificativa_aprovada(vigente):
            raise _regra_violada(SEM_APROVACAO_PARA_DESFAZER)
        if not _pode_desfazer_aprovacao(db, usuario, vigente.aprovacao):
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=SO_QUEM_APROVOU)

    # Com a aprovação ligada, a justificativa aprovada em vigor só é alterada
    # (justificar de novo, conferir) ou retirada depois que quem aprovou desfaz
    # a aprovação (issue #164). Com a flag desligada, nada muda.
    if corpo.tipo in TIPOS_QUE_TIRAM_A_APROVACAO and settings.aprovacao_justificativas_habilitada:
        eventos = decisoes.carregar_eventos(db, empresa_id, extrato_id, {corpo.chave})
        if decisoes.justificativa_aprovada(decisoes.decisao_vigente(eventos.get(corpo.chave, []))):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT, detail=JUSTIFICATIVA_APROVADA
            )

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
    if corpo.tipo == "justificada" and not settings.aprovacao_justificativas_habilitada:
        _aprovar_junto(db, evento)
    db.commit()

    if corpo.tipo in decisoes.TIPOS_DESFAZER or corpo.tipo == "justificativa_rejeitada":
        return None
    eventos = decisoes.carregar_eventos(db, empresa_id, extrato_id, {corpo.chave})
    return _decisao_response(decisoes.decisao_vigente(eventos.get(corpo.chave, [])))
