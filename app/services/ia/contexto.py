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

Uma divergência pode ter uma ponta só ou, em `divergente_data`, um par
gravado pelo motor. O contexto do par usa a linha bancária como item e a
ponta gravada do sistema como candidata.

Exclusão de candidatos já casados (ADR-011): um lançamento do outro lado
que já é `lancamento_banco_id`/`lancamento_sistema_id` de uma linha
`match_exato`/`match_tolerancia` do MESMO par de extratos nunca entra como
candidato de `divergente_valor`/`divergente_data` — sugerir à IA um
lançamento que já tem par confirmado seria enganoso. Isso é seguro de
implementar porque as linhas de match guardam os dois lados
explicitamente (`Conciliacao.lancamento_banco_id` e
`lancamento_sistema_id` não nulos), então a consulta é uma simples
exclusão por id, sem ambiguidade. O `match_agrupado` (issue #131) também
consome o lançamento, mas guarda só a ponta dele: a mesma exclusão por id,
olhando a coluna do outro lado. O par com divergência de data também reserva
as suas pontas, embora continue elegível a uma explicação própria.

A ordem dos candidatos é 100% determinística (entra no hash da chave de
cache, app/services/ia/cache.py): ordenados em Python (mesmo estilo de
`app/services/matching.py`, que também ordena listas em memória em vez de
depender de `ORDER BY`) pela diferença (valor ou dias) e desempatados por
(data, valor, tipo, descrição).

Fatos calculados (fix da explicação detalhada): além dos candidatos, o
contexto leva o lado do item, a tolerância da rodada (a da execução mais
recente do par em `execucoes_conciliacao`, pelo mesmo critério de
app/services/fechamentos.py, `criado_em` e depois `id` decrescentes; sem
execução, a `Configuracao` da empresa; sem nenhuma, `None`), o total de
candidatos antes do corte, o período do extrato do outro lado e, por
candidato, os dias com sinal, a diferença percentual, a similaridade das
descrições (a mesma função do motor) e se o intervalo cruza fim de semana.
Tudo calculado aqui, sob demanda, sem coluna nova: a IA só interpreta
(ADR-011). A tolerância e os períodos são constantes no par: a geração
automática calcula um `FatosDoPar` uma vez (`calcular_fatos_do_par`) e o
repassa para cada linha; sem ele, cada chamada calcula o que precisa, como
no POST e no GET de detalhes. O resultado é o mesmo nos dois caminhos.

Nunca inclui `id` de lançamento no contexto (`LancamentoContexto` não tem
esse campo). Toda consulta filtra por `empresa_id` (ADR-004) — nunca lê
lançamento de outra empresa, mesmo que o extrato_id estivesse certo.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Conciliacao, Configuracao, ExecucaoConciliacao, Extrato, Lancamento
from app.services.ia.base import CandidatoContexto, ContextoDivergencia, LancamentoContexto
from app.services.ia.prompt import motivo_deterministico
from app.services.matching import (
    STATUS_DIVERGENTE_DATA,
    STATUS_DIVERGENTE_VALOR,
    STATUS_DUPLICADO,
    STATUS_MATCH_AGRUPADO,
    STATUS_MATCH_EXATO,
    STATUS_MATCH_TOLERANCIA,
    STATUS_SEM_CORRESPONDENCIA,
    STATUS_TARIFA_BANCARIA,
    similaridade_descricao,
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
    coluna_do_outro_lado). Se há duas pontas, o banco é a referência."""
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
    """Ids do outro lado já associados pelo motor no mesmo par de extratos."""
    linhas = db.execute(
        select(coluna_outro_lado).where(
            Conciliacao.empresa_id == conciliacao.empresa_id,
            Conciliacao.extrato_banco_id == conciliacao.extrato_banco_id,
            Conciliacao.extrato_sistema_id == conciliacao.extrato_sistema_id,
            or_(
                Conciliacao.status.in_(
                    (STATUS_MATCH_EXATO, STATUS_MATCH_TOLERANCIA, STATUS_MATCH_AGRUPADO)
                ),
                and_(
                    Conciliacao.status == STATUS_DIVERGENTE_DATA,
                    Conciliacao.lancamento_banco_id.isnot(None),
                    Conciliacao.lancamento_sistema_id.isnot(None),
                ),
            ),
            coluna_outro_lado.isnot(None),
        )
    ).all()
    return {linha[0] for linha in linhas}


def _tolerancia_da_rodada(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID, extrato_sistema_id: uuid.UUID
) -> int | None:
    """A tolerância efetivamente aplicada na execução mais recente do par
    (mesmo critério de "rodada atual" de app/services/fechamentos.py); sem
    execução, a configuração atual da empresa; sem nenhuma, `None`."""
    tolerancia = db.scalar(
        select(ExecucaoConciliacao.tolerancia_dias)
        .where(
            ExecucaoConciliacao.empresa_id == empresa_id,
            ExecucaoConciliacao.extrato_banco_id == extrato_banco_id,
            ExecucaoConciliacao.extrato_sistema_id == extrato_sistema_id,
        )
        .order_by(ExecucaoConciliacao.criado_em.desc(), ExecucaoConciliacao.id.desc())
        .limit(1)
    )
    if tolerancia is not None:
        return tolerancia
    return db.scalar(
        select(Configuracao.tolerancia_dias_default).where(Configuracao.empresa_id == empresa_id)
    )


def _periodo_do_extrato(
    db: Session, empresa_id: uuid.UUID, extrato_id: uuid.UUID
) -> tuple[date, date] | None:
    """O período gravado do extrato (issue #82) ou, sem ele, a menor e a
    maior data dos lançamentos."""
    inicio, fim = db.execute(
        select(Extrato.periodo_inicio, Extrato.periodo_fim).where(
            Extrato.id == extrato_id, Extrato.empresa_id == empresa_id
        )
    ).one_or_none() or (None, None)
    if inicio is not None and fim is not None:
        return (inicio, fim)
    inicio, fim = db.execute(
        select(func.min(Lancamento.data), func.max(Lancamento.data)).where(
            Lancamento.empresa_id == empresa_id, Lancamento.extrato_id == extrato_id
        )
    ).one()
    if inicio is None or fim is None:
        return None
    return (inicio, fim)


@dataclass(frozen=True)
class FatosDoPar:
    """O que é constante num par de extratos: a tolerância da rodada e o
    período de cada extrato. Leva os ids do par para nunca ser usado em
    outro (`montar_contexto_divergencia` recalcula se não bater)."""

    empresa_id: uuid.UUID
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    tolerancia_dias: int | None
    periodo_banco: tuple[date, date] | None
    periodo_sistema: tuple[date, date] | None


def calcular_fatos_do_par(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID, extrato_sistema_id: uuid.UUID
) -> FatosDoPar:
    return FatosDoPar(
        empresa_id=empresa_id,
        extrato_banco_id=extrato_banco_id,
        extrato_sistema_id=extrato_sistema_id,
        tolerancia_dias=_tolerancia_da_rodada(db, empresa_id, extrato_banco_id, extrato_sistema_id),
        periodo_banco=_periodo_do_extrato(db, empresa_id, extrato_banco_id),
        periodo_sistema=_periodo_do_extrato(db, empresa_id, extrato_sistema_id),
    )


def _fatos_gerais(
    db: Session, conciliacao: Conciliacao, extrato_outro_id: uuid.UUID, fatos_do_par
) -> dict:
    lado = "banco" if conciliacao.lancamento_banco_id is not None else "sistema"
    if fatos_do_par is not None and (
        fatos_do_par.empresa_id,
        fatos_do_par.extrato_banco_id,
        fatos_do_par.extrato_sistema_id,
    ) == (conciliacao.empresa_id, conciliacao.extrato_banco_id, conciliacao.extrato_sistema_id):
        periodo = fatos_do_par.periodo_sistema if lado == "banco" else fatos_do_par.periodo_banco
        return {
            "lado_do_item": lado,
            "tolerancia_dias": fatos_do_par.tolerancia_dias,
            "periodo_outro_lado": periodo,
        }
    return {
        "lado_do_item": lado,
        "tolerancia_dias": _tolerancia_da_rodada(
            db,
            conciliacao.empresa_id,
            conciliacao.extrato_banco_id,
            conciliacao.extrato_sistema_id,
        ),
        "periodo_outro_lado": _periodo_do_extrato(db, conciliacao.empresa_id, extrato_outro_id),
    }


def cruza_fim_de_semana(data_a: date, data_b: date) -> bool:
    """Alguma data do intervalo fechado entre as duas é sábado ou domingo.
    Sem calendário de feriados."""
    inicio, fim = sorted((data_a, data_b))
    if (fim - inicio).days >= 6:
        return True
    dia = inicio
    while dia <= fim:
        if dia.weekday() >= 5:
            return True
        dia += timedelta(days=1)
    return False


def _percentual(diferenca: Decimal, valor_item: Decimal) -> Decimal | None:
    if valor_item == 0:
        return None
    return (abs(diferenca) / abs(valor_item) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _similaridade(item: Lancamento, candidato: Lancamento) -> int:
    return int(
        Decimal(str(similaridade_descricao(item.descricao, candidato.descricao))).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )


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


def montar_contexto_divergencia(
    db: Session, conciliacao: Conciliacao, fatos_do_par: FatosDoPar | None = None
) -> ContextoDivergencia:
    """Contexto pra `conciliacao` (ADR-011). Levanta `ValueError` (a mesma
    exceção de `motivo_deterministico`) pra status não elegível
    (`match_exato`, `match_tolerancia`, `match_agrupado` ou desconhecido) — chamado como
    primeiro passo, antes de qualquer consulta, pra falhar rápido.
    `fatos_do_par`, opcional, evita recalcular a tolerância e o período a
    cada linha do mesmo par (geração automática)."""
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
    fatos_gerais = _fatos_gerais(db, conciliacao, extrato_outro_id, fatos_do_par)

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
            **fatos_gerais,
        )

    if conciliacao.status in (STATUS_TARIFA_BANCARIA, STATUS_SEM_CORRESPONDENCIA):
        return ContextoDivergencia(
            status=conciliacao.status,
            motivo=motivo,
            lancamento=lancamento_ctx,
            candidatos=(),
            quantidade_mesmo_lado=None,
            quantidade_outro_lado=None,
            **fatos_gerais,
        )

    vinculado = (
        conciliacao.lancamento_sistema_id
        if conciliacao.status == STATUS_DIVERGENTE_DATA and conciliacao.lancamento_banco_id
        else None
    )
    ja_casados = set() if vinculado else _ids_ja_casados(db, conciliacao, coluna_outro_lado)

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
                diferenca_percentual=_percentual(l.valor - item.valor, item.valor),
                similaridade_descricao=_similaridade(item, l),
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
            total_candidatos=len(candidatos_lancamentos),
            par_gravado=False,
            **fatos_gerais,
        )

    if conciliacao.status == STATUS_DIVERGENTE_DATA:
        consulta = select(Lancamento).where(
            Lancamento.empresa_id == conciliacao.empresa_id,
            Lancamento.extrato_id == extrato_outro_id,
            Lancamento.valor == item.valor,
            Lancamento.data != item.data,
        )
        if vinculado:
            consulta = consulta.where(Lancamento.id == vinculado)
        elif ja_casados:
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
                dias_assinados=(l.data - item.data).days,
                similaridade_descricao=_similaridade(item, l),
                cruza_fim_de_semana=cruza_fim_de_semana(item.data, l.data),
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
            total_candidatos=len(candidatos_lancamentos),
            par_gravado=vinculado is not None,
            **fatos_gerais,
        )

    # Inalcançável: motivo_deterministico já filtrou pras 5 categorias
    # elegíveis, e todas as 5 são tratadas nos ramos acima.
    raise AssertionError(f"status inesperado depois da validação: {conciliacao.status!r}")
