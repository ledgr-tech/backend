"""Monta o `ContextoDivergencia` (app/services/ia/base.py) de uma linha de
`conciliacoes` já persistida (issue #28, ADR-011).

Reusa os critérios de "mesma data"/"mesmo valor"/"duplicado" de
`app/services/matching.py` (as mesmas constantes `STATUS_*`, e a mesma
semântica de igualdade simples — sem normalização — que
`_classificar_remanescente` usa lá: `item.data in datas_do_outro_lado`,
`item.valor in valores_do_outro_lado`). Não há uma função separada pra
extrair: em `matching.py` essas comparações são checagens de pertencimento
em `set` inline, não uma função própria, então "reusar" aqui significa
replicar a mesma igualdade direta (Decimal `==` Decimal, `date` `==`
`date`) nas cláusulas `WHERE`, e importar as constantes de status em vez de
redeclarar as strings.

Uma linha de `conciliacoes` de divergência guarda só UM lado (o outro fica
NULL) — `_lado_do_item` abaixo decide de qual lado é o item e qual é o
extrato do lado oposto, a partir de qual coluna de lançamento está
preenchida.

Exclusão de candidatos já casados (ADR-011): um lançamento do outro lado
que já é `lancamento_banco_id`/`lancamento_sistema_id` de uma linha
`match_exato`/`match_tolerancia` do MESMO par de extratos nunca entra como
candidato de `divergente_valor`/`divergente_data` — sugerir à IA um
lançamento que já tem par confirmado seria enganoso. Isso é seguro de
implementar porque as linhas de match guardam os dois lados
explicitamente (`Conciliacao.lancamento_banco_id` e
`lancamento_sistema_id` não nulos), então a consulta é uma simples
exclusão por id, sem ambiguidade.

A ordem dos candidatos é 100% determinística (entra no hash da chave de
cache, app/services/ia/cache.py): ordenados em Python (mesmo estilo de
`app/services/matching.py`, que também ordena listas em memória em vez de
depender de `ORDER BY`) pela diferença (valor ou dias) e desempatados por
(data, valor, tipo, descrição).

Nunca inclui `id` de lançamento no contexto (`LancamentoContexto` não tem
esse campo). Toda consulta filtra por `empresa_id` (ADR-004) — nunca lê
lançamento de outra empresa, mesmo que o extrato_id estivesse certo.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Conciliacao, Lancamento
from app.services.ia.base import CandidatoContexto, ContextoDivergencia, LancamentoContexto
from app.services.ia.prompt import motivo_deterministico
from app.services.matching import (
    STATUS_DIVERGENTE_DATA,
    STATUS_DIVERGENTE_VALOR,
    STATUS_DUPLICADO,
    STATUS_MATCH_EXATO,
    STATUS_MATCH_TOLERANCIA,
    STATUS_SEM_CORRESPONDENCIA,
    STATUS_TARIFA_BANCARIA,
)

MAXIMO_CANDIDATOS = 3


def _lancamento_contexto(lancamento: Lancamento) -> LancamentoContexto:
    return LancamentoContexto(
        data=lancamento.data,
        valor=lancamento.valor,
        tipo=lancamento.tipo,
        descricao=lancamento.descricao,
    )


def _chave_desempate(lancamento: Lancamento) -> tuple:
    return (lancamento.data, lancamento.valor, lancamento.tipo, lancamento.descricao)


def _lado_do_item(conciliacao: Conciliacao) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, object]:
    """(extrato_do_item, extrato_do_outro_lado, lancamento_id_do_item,
    coluna_do_outro_lado) — a linha de divergência guarda só um lado."""
    if conciliacao.lancamento_banco_id is not None:
        return (
            conciliacao.extrato_banco_id,
            conciliacao.extrato_sistema_id,
            conciliacao.lancamento_banco_id,
            Conciliacao.lancamento_sistema_id,
        )
    return (
        conciliacao.extrato_sistema_id,
        conciliacao.extrato_banco_id,
        conciliacao.lancamento_sistema_id,
        Conciliacao.lancamento_banco_id,
    )


def _ids_ja_casados(db: Session, conciliacao: Conciliacao, coluna_outro_lado) -> set[uuid.UUID]:
    """Ids de lançamento do outro lado já consumidos por um
    match_exato/match_tolerancia do mesmo par de extratos (ver docstring do
    módulo) — nunca entram como candidato de divergência."""
    linhas = db.execute(
        select(coluna_outro_lado).where(
            Conciliacao.empresa_id == conciliacao.empresa_id,
            Conciliacao.extrato_banco_id == conciliacao.extrato_banco_id,
            Conciliacao.extrato_sistema_id == conciliacao.extrato_sistema_id,
            Conciliacao.status.in_((STATUS_MATCH_EXATO, STATUS_MATCH_TOLERANCIA)),
            coluna_outro_lado.isnot(None),
        )
    ).all()
    return {linha[0] for linha in linhas}


def _contar_mesmo_valor_e_data(
    db: Session, empresa_id: uuid.UUID, extrato_id: uuid.UUID, valor: Decimal, data
) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Lancamento)
        .where(
            Lancamento.empresa_id == empresa_id,
            Lancamento.extrato_id == extrato_id,
            Lancamento.valor == valor,
            Lancamento.data == data,
        )
    )


def montar_contexto_divergencia(db: Session, conciliacao: Conciliacao) -> ContextoDivergencia:
    """Contexto pra `conciliacao` (ADR-011). Levanta `ValueError` (a mesma
    exceção de `motivo_deterministico`) pra status não elegível
    (`match_exato`, `match_tolerancia` ou desconhecido) — chamado como
    primeiro passo, antes de qualquer consulta, pra falhar rápido."""
    motivo = motivo_deterministico(conciliacao.status)

    extrato_item_id, extrato_outro_id, lancamento_item_id, coluna_outro_lado = _lado_do_item(
        conciliacao
    )
    item = db.execute(
        select(Lancamento).where(
            Lancamento.id == lancamento_item_id,
            Lancamento.empresa_id == conciliacao.empresa_id,
        )
    ).scalar_one()
    lancamento_ctx = _lancamento_contexto(item)

    if conciliacao.status == STATUS_DUPLICADO:
        quantidade_mesmo_lado = _contar_mesmo_valor_e_data(
            db, conciliacao.empresa_id, extrato_item_id, item.valor, item.data
        )
        quantidade_outro_lado = _contar_mesmo_valor_e_data(
            db, conciliacao.empresa_id, extrato_outro_id, item.valor, item.data
        )
        return ContextoDivergencia(
            status=conciliacao.status,
            motivo=motivo,
            lancamento=lancamento_ctx,
            candidatos=(),
            quantidade_mesmo_lado=quantidade_mesmo_lado,
            quantidade_outro_lado=quantidade_outro_lado,
        )

    if conciliacao.status in (STATUS_TARIFA_BANCARIA, STATUS_SEM_CORRESPONDENCIA):
        return ContextoDivergencia(
            status=conciliacao.status,
            motivo=motivo,
            lancamento=lancamento_ctx,
            candidatos=(),
            quantidade_mesmo_lado=None,
            quantidade_outro_lado=None,
        )

    ja_casados = _ids_ja_casados(db, conciliacao, coluna_outro_lado)

    if conciliacao.status == STATUS_DIVERGENTE_VALOR:
        consulta = select(Lancamento).where(
            Lancamento.empresa_id == conciliacao.empresa_id,
            Lancamento.extrato_id == extrato_outro_id,
            Lancamento.data == item.data,
            Lancamento.valor != item.valor,
        )
        if ja_casados:
            consulta = consulta.where(Lancamento.id.not_in(ja_casados))
        candidatos_lancamentos = db.execute(consulta).scalars().all()
        candidatos_ordenados = sorted(
            candidatos_lancamentos,
            key=lambda l: (abs(l.valor - item.valor), *_chave_desempate(l)),
        )[:MAXIMO_CANDIDATOS]
        candidatos = tuple(
            CandidatoContexto(
                lancamento=_lancamento_contexto(l),
                diferenca_valor=abs(l.valor - item.valor),
                diferenca_dias=None,
            )
            for l in candidatos_ordenados
        )
        return ContextoDivergencia(
            status=conciliacao.status,
            motivo=motivo,
            lancamento=lancamento_ctx,
            candidatos=candidatos,
            quantidade_mesmo_lado=None,
            quantidade_outro_lado=None,
        )

    if conciliacao.status == STATUS_DIVERGENTE_DATA:
        consulta = select(Lancamento).where(
            Lancamento.empresa_id == conciliacao.empresa_id,
            Lancamento.extrato_id == extrato_outro_id,
            Lancamento.valor == item.valor,
            Lancamento.data != item.data,
        )
        if ja_casados:
            consulta = consulta.where(Lancamento.id.not_in(ja_casados))
        candidatos_lancamentos = db.execute(consulta).scalars().all()
        candidatos_ordenados = sorted(
            candidatos_lancamentos,
            key=lambda l: (abs((l.data - item.data).days), *_chave_desempate(l)),
        )[:MAXIMO_CANDIDATOS]
        candidatos = tuple(
            CandidatoContexto(
                lancamento=_lancamento_contexto(l),
                diferenca_valor=None,
                diferenca_dias=abs((l.data - item.data).days),
            )
            for l in candidatos_ordenados
        )
        return ContextoDivergencia(
            status=conciliacao.status,
            motivo=motivo,
            lancamento=lancamento_ctx,
            candidatos=candidatos,
            quantidade_mesmo_lado=None,
            quantidade_outro_lado=None,
        )

    # Inalcançável: motivo_deterministico já filtrou pras 5 categorias
    # elegíveis, e todas as 5 são tratadas nos ramos acima.
    raise AssertionError(f"status inesperado depois da validação: {conciliacao.status!r}")
