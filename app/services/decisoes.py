"""Regras das decisões por linha (issue #83): chave da linha, rodada, decisão
em vigor e contagem de justificadas. Único lugar com essas regras, usado por
`GET /conciliacoes/{extrato_id}`, `POST /conciliacoes/{extrato_id}/decisoes`
e `GET /execucoes`.

Uma linha "diverge" quando o status não é `match_exato` nem
`match_tolerancia` (as cinco categorias de divergência), mesma regra de
`estaResolvida` no frontend. Só linha divergente aceita decisão.

Aprovação (issue #132, ADR-019): toda justificativa nasce aguardando
aprovação. `justificativa_aprovada` marca a justificada em vigor como
aprovada; `justificativa_rejeitada` e `justificativa_desfeita` voltam a linha
para nenhuma decisão. Justificar de novo cria uma justificativa nova,
aguardando aprovação, mesmo que a anterior estivesse aprovada. Nas contagens,
"justificadas" são só as aprovadas, "aguardando aprovação" são as pendentes, e
a divergência com justificativa pendente não é "sem justificativa".

Rodada: a rodada N de um extrato do banco é o N-ésimo extrato do sistema
conciliado com ele, na ordem da PRIMEIRA execução de cada par em
`execucoes_conciliacao`. Reconciliar o mesmo par não cria rodada nova, e a
rodada mais recente é a de maior N, não a execução mais nova (reconciliar a
v1 depois da v2 não torna a v1 a rodada de agora).
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.models import Conciliacao, DecisaoLinha, ExecucaoConciliacao, Lancamento
from app.services.normalizacao import _normalizar_descricao

STATUS_RESOLVIDOS = frozenset({"match_exato", "match_tolerancia"})

TIPOS_DECISAO = ("conferida", "justificada")
TIPOS_DESFAZER = {"conferencia_desfeita": "conferida", "justificativa_desfeita": "justificada"}
# Aprovar e rejeitar a justificativa em vigor (issue #132): só admin e gestor.
TIPOS_APROVACAO = ("justificativa_aprovada", "justificativa_rejeitada")
AUTOR_APROVACAO_DA_MIGRACAO = "Aprovada na atualização de 21/10"

SituacaoJustificativa = Literal["pendente", "aprovada"]


def diverge(status: str) -> bool:
    return status not in STATUS_RESOLVIDOS


def chave_da_linha(
    lancamento_banco_id: uuid.UUID | None,
    sistema_valor: Decimal | None,
    sistema_data: date | None,
    sistema_descricao: str | None,
    sistema_ocorrencia: int | None,
) -> str:
    """Identificador da linha que sobrevive às rodadas.

    Com lançamento do banco, é o id dele: o extrato do banco não muda entre
    rodadas. Linha só do sistema: sha256 de valor, data, descrição normalizada
    (a mesma `_normalizar_descricao` da normalização) e `ocorrencia`. Não usa
    `hash_dedup` nem o id do lançamento do sistema, que mudam a cada versão do
    extrato do sistema; a mesma linha em duas versões tem a mesma chave.

    É única dentro de um par: cada lançamento do banco aparece em uma linha
    só, e duas linhas idênticas do sistema no mesmo arquivo têm `ocorrencia`
    diferente. As duas formas não colidem (UUID com hífens x 64 hex).
    """
    if lancamento_banco_id is not None:
        return str(lancamento_banco_id)
    bruto = (
        f"{sistema_valor}|{sistema_data.isoformat()}|"
        f"{_normalizar_descricao(sistema_descricao)}|{sistema_ocorrencia}"
    )
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Rodada:
    extrato_sistema_id: uuid.UUID
    numero: int


def rodadas_mais_recentes(
    db: Session, empresa_id: uuid.UUID, extratos_banco_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Rodada]:
    """A rodada de maior número de cada extrato do banco, numa consulta só.
    Extrato do banco nunca conciliado fica fora do dicionário."""
    if not extratos_banco_ids:
        return {}
    primeira = func.min(ExecucaoConciliacao.criado_em)
    pares = db.execute(
        select(ExecucaoConciliacao.extrato_banco_id, ExecucaoConciliacao.extrato_sistema_id)
        .where(
            ExecucaoConciliacao.empresa_id == empresa_id,
            ExecucaoConciliacao.extrato_banco_id.in_(list(extratos_banco_ids)),
        )
        .group_by(ExecucaoConciliacao.extrato_banco_id, ExecucaoConciliacao.extrato_sistema_id)
        .order_by(
            ExecucaoConciliacao.extrato_banco_id, primeira, ExecucaoConciliacao.extrato_sistema_id
        )
    ).all()
    por_banco: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for par in pares:
        por_banco[par.extrato_banco_id].append(par.extrato_sistema_id)
    return {
        banco: Rodada(extrato_sistema_id=sistemas[-1], numero=len(sistemas))
        for banco, sistemas in por_banco.items()
    }


def rodada_mais_recente(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID
) -> Rodada | None:
    """A rodada de maior número do extrato do banco, numa consulta só, ou
    `None` quando ele nunca foi conciliado."""
    return rodadas_mais_recentes(db, empresa_id, [extrato_banco_id]).get(extrato_banco_id)


def carregar_eventos(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID, chaves: set[str]
) -> dict[str, list[DecisaoLinha]]:
    """Eventos de um conjunto de chaves de um extrato do banco, numa consulta
    só, em ordem cronológica crescente (`em`, `id`)."""
    if not chaves:
        return {}
    eventos = db.scalars(
        select(DecisaoLinha)
        .where(
            DecisaoLinha.empresa_id == empresa_id,
            DecisaoLinha.extrato_banco_id == extrato_banco_id,
            DecisaoLinha.chave.in_(sorted(chaves)),
        )
        .order_by(DecisaoLinha.em, DecisaoLinha.id)
    ).all()
    por_chave: dict[str, list[DecisaoLinha]] = defaultdict(list)
    for evento in eventos:
        por_chave[evento.chave].append(evento)
    return por_chave


@dataclass(frozen=True)
class DecisaoEmVigor:
    """A decisão em vigor de uma linha: o evento `conferida` ou `justificada`
    e, na justificada aprovada, o evento da aprovação."""

    evento: DecisaoLinha
    aprovacao: DecisaoLinha | None = None

    @property
    def tipo(self) -> str:
        return self.evento.tipo

    @property
    def situacao(self) -> SituacaoJustificativa | None:
        """`pendente` ou `aprovada` na justificada; `None` na conferida."""
        if self.evento.tipo != "justificada":
            return None
        return "aprovada" if self.aprovacao is not None else "pendente"


def decisao_vigente(eventos: list[DecisaoLinha]) -> DecisaoEmVigor | None:
    """`conferida` e `justificada` passam a valer (a justificada, aguardando
    aprovação); `justificativa_aprovada` aprova a justificada pendente em
    vigor; a rejeição e os dois "desfeita" voltam a nenhuma decisão.
    `eventos` em ordem cronológica."""
    vigente = None
    for evento in eventos:
        if evento.tipo in TIPOS_DECISAO:
            vigente = DecisaoEmVigor(evento)
        elif evento.tipo == "justificativa_aprovada":
            if vigente is not None and vigente.situacao == "pendente":
                vigente = DecisaoEmVigor(vigente.evento, aprovacao=evento)
        else:
            vigente = None
    return vigente


def aguardando_aprovacao(vigente: DecisaoEmVigor | None) -> bool:
    return vigente is not None and vigente.situacao == "pendente"


def justificativa_aprovada(vigente: DecisaoEmVigor | None) -> bool:
    return vigente is not None and vigente.situacao == "aprovada"


@dataclass(frozen=True)
class Evento:
    tipo: str
    texto: str | None
    autor: str
    em: datetime
    rodada: int


def como_evento(evento: DecisaoLinha) -> Evento:
    return Evento(
        tipo=evento.tipo,
        texto=evento.texto,
        autor=evento.autor_nome,
        em=evento.em,
        rodada=evento.rodada,
    )


def _vigentes_das_linhas(
    db: Session,
    empresa_id: uuid.UUID,
    pares: set[tuple[uuid.UUID, uuid.UUID]],
) -> dict[tuple[uuid.UUID, uuid.UUID], list[tuple[str, DecisaoEmVigor | None]]]:
    """A chave e a decisão em vigor de cada linha divergente de cada par
    `(extrato_banco_id, extrato_sistema_id)`, valendo decisão de qualquer
    rodada. Duas consultas, qualquer que seja o número de pares: as linhas
    divergentes de todos os pares e os eventos dos extratos do banco
    envolvidos."""
    if not pares:
        return {}
    lanc_sistema = aliased(Lancamento)
    linhas = db.execute(
        select(
            Conciliacao.extrato_banco_id,
            Conciliacao.extrato_sistema_id,
            Conciliacao.lancamento_banco_id,
            lanc_sistema.valor,
            lanc_sistema.data,
            lanc_sistema.descricao,
            lanc_sistema.ocorrencia,
        )
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(
            Conciliacao.empresa_id == empresa_id,
            tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(list(pares)),
            Conciliacao.status.not_in(sorted(STATUS_RESOLVIDOS)),
        )
    ).all()
    chaves_por_par: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = defaultdict(list)
    for linha in linhas:
        chaves_por_par[(linha.extrato_banco_id, linha.extrato_sistema_id)].append(
            chave_da_linha(
                linha.lancamento_banco_id,
                linha.valor,
                linha.data,
                linha.descricao,
                linha.ocorrencia,
            )
        )
    if not chaves_por_par:
        return {}

    # Sem filtro por chave: a página pode ter chaves divergentes demais para o
    # limite de 65535 parâmetros por statement do psycopg 3. Os eventos de
    # chaves que não estão nos pares são ignorados abaixo.
    bancos = {banco for banco, _ in chaves_por_par}
    eventos = db.scalars(
        select(DecisaoLinha)
        .where(
            DecisaoLinha.empresa_id == empresa_id,
            DecisaoLinha.extrato_banco_id.in_(list(bancos)),
        )
        .order_by(DecisaoLinha.em, DecisaoLinha.id)
    ).all()
    por_banco_e_chave: dict[tuple[uuid.UUID, str], list[DecisaoLinha]] = defaultdict(list)
    for evento in eventos:
        por_banco_e_chave[(evento.extrato_banco_id, evento.chave)].append(evento)

    return {
        (banco, sistema): [
            (chave, decisao_vigente(por_banco_e_chave.get((banco, chave), []))) for chave in lista
        ]
        for (banco, sistema), lista in chaves_por_par.items()
    }


@dataclass(frozen=True)
class ContagemJustificativas:
    aprovadas: int
    aguardando_aprovacao: int


def contar_justificativas(
    db: Session,
    empresa_id: uuid.UUID,
    pares: set[tuple[uuid.UUID, uuid.UUID]],
) -> dict[tuple[uuid.UUID, uuid.UUID], ContagemJustificativas]:
    """Linhas divergentes de cada par com justificativa aprovada e com
    justificativa aguardando aprovação. Duas consultas (`_vigentes_das_linhas`)."""
    return {
        par: ContagemJustificativas(
            aprovadas=sum(1 for _, vigente in linhas if justificativa_aprovada(vigente)),
            aguardando_aprovacao=sum(1 for _, vigente in linhas if aguardando_aprovacao(vigente)),
        )
        for par, linhas in _vigentes_das_linhas(db, empresa_id, pares).items()
    }


def justificativas_aguardando(
    db: Session,
    empresa_id: uuid.UUID,
    pares: set[tuple[uuid.UUID, uuid.UUID]],
) -> dict[tuple[uuid.UUID, str], DecisaoEmVigor]:
    """As linhas divergentes dos pares com justificativa aguardando aprovação,
    por `(extrato_banco_id, chave)`: o que a aprovação em lote processa."""
    return {
        (banco, chave): vigente
        for (banco, _), linhas in _vigentes_das_linhas(db, empresa_id, pares).items()
        for chave, vigente in linhas
        if aguardando_aprovacao(vigente)
    }
