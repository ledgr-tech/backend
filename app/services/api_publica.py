"""Consultas da API pública (issue #135, ADR-021), só de leitura.

As regras são as mesmas do app, importadas de onde moram: os pares do mês e as
pendências do fechamento (app/services/fechamentos.py) e a decisão em vigor de
cada linha (app/services/decisoes.py). O que a API nunca devolve: descrição de
lançamento, texto de justificativa, texto da ressalva e nome de pessoa. A
descrição do lançamento do sistema é lida só para calcular a chave da linha,
como no app, e não sai daqui.

Número fixo de consultas, qualquer que seja o tamanho do mês ou da página.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, aliased

from app.models import Conciliacao, DecisaoLinha, Extrato, Lancamento
from app.services import decisoes, fechamentos

Situacao = Literal["sem_justificativa", "aguardando_aprovacao", "justificada", "conferida"]
Lado = Literal["banco", "sistema", "ambos"]


def situacao_da_linha(vigente: decisoes.DecisaoEmVigor | None) -> Situacao:
    if vigente is None:
        return "sem_justificativa"
    if vigente.tipo == "conferida":
        return "conferida"
    return "justificada" if vigente.situacao == "aprovada" else "aguardando_aprovacao"


@dataclass(frozen=True)
class Divergencia:
    extrato_banco_id: uuid.UUID
    chave: str
    categoria: str
    data: date
    valor: Decimal
    lado: Lado
    situacao: Situacao


def divergencias_do_mes(
    db: Session, empresa_id: uuid.UUID, pares: list[fechamentos.ParDoMes]
) -> list[Divergencia]:
    """As linhas divergentes dos pares vigentes, com a situação de cada uma,
    em ordem de data, valor, extrato e chave. Duas consultas."""
    if not pares:
        return []
    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    linhas = db.execute(
        select(
            Conciliacao.extrato_banco_id,
            Conciliacao.status,
            Conciliacao.lancamento_banco_id,
            Conciliacao.lancamento_sistema_id,
            lanc_banco.data.label("banco_data"),
            lanc_banco.valor.label("banco_valor"),
            lanc_sistema.data.label("sistema_data"),
            lanc_sistema.valor.label("sistema_valor"),
            lanc_sistema.descricao.label("sistema_descricao"),
            lanc_sistema.ocorrencia.label("sistema_ocorrencia"),
        )
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(
            Conciliacao.empresa_id == empresa_id,
            tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(
                [(par.extrato_banco_id, par.extrato_sistema_id) for par in pares]
            ),
            Conciliacao.status.not_in(sorted(decisoes.STATUS_RESOLVIDOS)),
        )
    ).all()

    eventos: dict[tuple[uuid.UUID, str], list[DecisaoLinha]] = defaultdict(list)
    for evento in db.scalars(
        select(DecisaoLinha)
        .where(
            DecisaoLinha.empresa_id == empresa_id,
            DecisaoLinha.extrato_banco_id.in_([par.extrato_banco_id for par in pares]),
        )
        .order_by(DecisaoLinha.em, DecisaoLinha.id)
    ):
        eventos[(evento.extrato_banco_id, evento.chave)].append(evento)

    resultado = []
    for linha in linhas:
        chave = decisoes.chave_da_linha(
            linha.lancamento_banco_id,
            linha.sistema_valor,
            linha.sistema_data,
            linha.sistema_descricao,
            linha.sistema_ocorrencia,
        )
        tem_banco = linha.lancamento_banco_id is not None
        tem_sistema = linha.lancamento_sistema_id is not None
        resultado.append(
            Divergencia(
                extrato_banco_id=linha.extrato_banco_id,
                chave=chave,
                categoria=linha.status,
                data=linha.banco_data if tem_banco else linha.sistema_data,
                valor=linha.banco_valor if tem_banco else linha.sistema_valor,
                lado=(
                    "ambos" if tem_banco and tem_sistema else ("banco" if tem_banco else "sistema")
                ),
                situacao=situacao_da_linha(
                    decisoes.decisao_vigente(eventos.get((linha.extrato_banco_id, chave), []))
                ),
            )
        )
    return sorted(resultado, key=lambda d: (d.data, d.valor, str(d.extrato_banco_id), d.chave))


def periodos_dos_extratos(
    db: Session, empresa_id: uuid.UUID, extratos_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[date | None, date | None]]:
    if not extratos_ids:
        return {}
    return {
        linha.id: (linha.periodo_inicio, linha.periodo_fim)
        for linha in db.execute(
            select(Extrato.id, Extrato.periodo_inicio, Extrato.periodo_fim).where(
                Extrato.empresa_id == empresa_id, Extrato.id.in_(extratos_ids)
            )
        )
    }
