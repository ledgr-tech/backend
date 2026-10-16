"""Saldo do extrato (issue #118): saldo inicial, saldo final e a data do saldo.

`saldo_final`/`saldo_data` são o que o arquivo informa, gravados pela
normalização (app/services/normalizacao.py). O saldo inicial nunca é
gravado: é calculado na leitura, como o saldo final menos a soma dos valores
(já com sinal) dos lançamentos do extrato com data até `saldo_data`,
inclusive. Lançamento depois de `saldo_data` não entra, porque o saldo do
arquivo é de antes dele.

O saldo inicial fica `None` quando o saldo final é nulo (arquivo sem saldo,
ainda processando ou extrato anterior à #118) e quando o extrato tem linhas
não lidas (`linhas_invalidas`): a soma estaria incompleta, e o número sairia
errado sem ninguém perceber.

Consultas: `saldo_do_extrato` faz uma só (linhas não lidas e soma juntas), e
nenhuma para extrato sem saldo. Para uma página de extratos,
`somar_lancamentos_ate_saldo_data` soma todos numa consulta agrupada, nunca
uma por extrato.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Extrato, Lancamento, LinhaInvalida


@dataclass(frozen=True)
class Saldo:
    saldo_inicial: Decimal | None
    saldo_final: Decimal | None
    saldo_data: date | None


SEM_SALDO = Saldo(saldo_inicial=None, saldo_final=None, saldo_data=None)


def montar_saldo(
    saldo_final: Decimal | None,
    saldo_data: date | None,
    linhas_nao_lidas: int,
    soma_ate_saldo_data: Decimal,
) -> Saldo:
    """Os três campos a partir das colunas, das linhas não lidas e da soma
    dos lançamentos até `saldo_data` (regra no docstring do módulo)."""
    if saldo_final is None or saldo_data is None:
        return SEM_SALDO
    saldo_inicial = None if linhas_nao_lidas else saldo_final - soma_ate_saldo_data
    return Saldo(saldo_inicial=saldo_inicial, saldo_final=saldo_final, saldo_data=saldo_data)


def somar_lancamentos_ate_saldo_data(
    db: Session, empresa_id: uuid.UUID, extrato_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Soma dos lançamentos de cada extrato com data até o `saldo_data` dele,
    numa consulta agrupada só. Extrato sem lançamento nesse intervalo fica
    fora do dicionário (soma zero); lista vazia não consulta nada."""
    if not extrato_ids:
        return {}
    linhas = db.execute(
        select(Lancamento.extrato_id, func.sum(Lancamento.valor))
        .join(Extrato, Extrato.id == Lancamento.extrato_id)
        .where(
            Lancamento.empresa_id == empresa_id,
            Lancamento.extrato_id.in_(extrato_ids),
            Lancamento.data <= Extrato.saldo_data,
        )
        .group_by(Lancamento.extrato_id)
    ).all()
    return {extrato_id: soma for extrato_id, soma in linhas}


def saldo_do_extrato(db: Session, extrato: Extrato) -> Saldo:
    """Saldo de um extrato: nenhuma consulta sem saldo final, senão uma."""
    if extrato.saldo_final is None or extrato.saldo_data is None:
        return SEM_SALDO
    linhas_nao_lidas = (
        select(func.count()).where(LinhaInvalida.extrato_id == extrato.id).scalar_subquery()
    )
    soma = (
        select(func.coalesce(func.sum(Lancamento.valor), 0))
        .where(
            Lancamento.empresa_id == extrato.empresa_id,
            Lancamento.extrato_id == extrato.id,
            Lancamento.data <= extrato.saldo_data,
        )
        .scalar_subquery()
    )
    contagem, total = db.execute(select(linhas_nao_lidas, soma)).one()
    return montar_saldo(extrato.saldo_final, extrato.saldo_data, contagem, total)
