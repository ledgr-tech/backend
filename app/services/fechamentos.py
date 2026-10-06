"""Fechamento do mês (issue #86, ADR-014): regra do mês, resumo congelado e a
guarda que bloqueia escritas em mês fechado.

Regra do mês, a mesma do front (`fechamento.ts` e `execucoesVigentes`):

- Competência de um extrato do banco é o mês de `periodo_inicio`
  (`app/services/travas.competencia_do_extrato`). Extrato que cruza dois meses
  entra no mês em que começa; sem período, não entra em mês nenhum.
- Os pares do mês são, para cada extrato do banco da competência, o par da
  rodada MAIS RECENTE dele (regra de `app/services/decisoes.py`), com a
  execução atual desse par. Rodadas anteriores não contam.
- Pendências: linhas divergentes desses pares sem justificativa em vigor, e
  linhas não lidas (`linhas_invalidas`) dos extratos do banco e do sistema
  dos pares, contando cada extrato uma vez. O mês está pronto quando não
  há nenhuma das duas.

Tudo com número fixo de consultas, qualquer que seja o número de extratos.
Nunca loga descrição de lançamento nem ressalva.
"""

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.models import (
    Conciliacao,
    DecisaoLinha,
    ExecucaoConciliacao,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
)
from app.services import decisoes
from app.services.travas import (
    competencia_do_extrato,
    travar_extrato_banco,
    travar_mes_compartilhada,
)

CATEGORIAS = (
    "total",
    "match_exato",
    "match_tolerancia",
    "duplicado",
    "sem_correspondencia",
    "tarifa_bancaria",
    "divergente_valor",
    "divergente_data",
)

_FORMATO_COMPETENCIA = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def competencia_valida(competencia: str) -> bool:
    return _FORMATO_COMPETENCIA.fullmatch(competencia) is not None


class MesFechadoError(Exception):
    def __init__(self, competencia: str) -> None:
        super().__init__(competencia)
        self.competencia = competencia


def fechamento_ativo(db: Session, empresa_id: uuid.UUID, competencia: str) -> Fechamento | None:
    return db.scalar(
        select(Fechamento).where(
            Fechamento.empresa_id == empresa_id,
            Fechamento.competencia == competencia,
            Fechamento.reaberto_em.is_(None),
        )
    )


def garantir_mes_aberto(db: Session, empresa_id: uuid.UUID, extrato_banco: Extrato) -> None:
    """Pega as travas na ordem fixa (mês compartilhada, se houver competência,
    depois a do extrato do banco) e levanta `MesFechadoError` se a
    competência do extrato do banco tiver fechamento ativo. Extrato sem
    período não tem competência: segue liberado."""
    competencia = competencia_do_extrato(extrato_banco)
    if competencia is not None:
        travar_mes_compartilhada(db, empresa_id, competencia)
    travar_extrato_banco(db, empresa_id, extrato_banco.id)
    if competencia is not None and fechamento_ativo(db, empresa_id, competencia) is not None:
        raise MesFechadoError(competencia)


@dataclass(frozen=True)
class ParDoMes:
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    rodada: int
    execucao: object
    nome_arquivo_banco: str
    nome_arquivo_sistema: str


def pares_do_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> list[ParDoMes]:
    """O par da rodada mais recente de cada extrato do banco da competência,
    com a execução atual dele. Três consultas."""
    bancos = {
        linha.id: linha.nome_arquivo
        for linha in db.execute(
            select(Extrato.id, Extrato.nome_arquivo).where(
                Extrato.empresa_id == empresa_id,
                Extrato.origem == "banco",
                func.to_char(Extrato.periodo_inicio, "YYYY-MM") == competencia,
            )
        )
    }
    rodadas = decisoes.rodadas_mais_recentes(db, empresa_id, list(bancos))
    if not rodadas:
        return []

    extrato_sistema = aliased(Extrato)
    execucoes = db.execute(
        select(ExecucaoConciliacao, extrato_sistema.nome_arquivo.label("nome_arquivo_sistema"))
        .join(extrato_sistema, ExecucaoConciliacao.extrato_sistema_id == extrato_sistema.id)
        .where(
            ExecucaoConciliacao.empresa_id == empresa_id,
            tuple_(
                ExecucaoConciliacao.extrato_banco_id, ExecucaoConciliacao.extrato_sistema_id
            ).in_([(banco, rodada.extrato_sistema_id) for banco, rodada in rodadas.items()]),
        )
        .order_by(
            ExecucaoConciliacao.extrato_banco_id,
            ExecucaoConciliacao.extrato_sistema_id,
            ExecucaoConciliacao.criado_em.desc(),
            ExecucaoConciliacao.id.desc(),
        )
        .distinct(ExecucaoConciliacao.extrato_banco_id, ExecucaoConciliacao.extrato_sistema_id)
    ).all()

    return sorted(
        (
            ParDoMes(
                extrato_banco_id=execucao.extrato_banco_id,
                extrato_sistema_id=execucao.extrato_sistema_id,
                rodada=rodadas[execucao.extrato_banco_id].numero,
                execucao=execucao,
                nome_arquivo_banco=bancos[execucao.extrato_banco_id],
                nome_arquivo_sistema=nome_arquivo_sistema,
            )
            for execucao, nome_arquivo_sistema in execucoes
        ),
        key=lambda par: (par.nome_arquivo_banco, str(par.extrato_banco_id)),
    )


def resumo_do_mes(db: Session, empresa_id: uuid.UUID, pares: list[ParDoMes]) -> dict:
    """Resumo congelado do mês (formato na docstring de app/api/fechamentos.py).
    Três consultas: linhas divergentes dos pares, eventos dos extratos do
    banco e linhas não lidas dos extratos envolvidos."""
    contagens = {categoria: 0 for categoria in CATEGORIAS}
    for par in pares:
        for categoria in CATEGORIAS:
            contagens[categoria] += getattr(par.execucao, categoria)

    justificadas = pendentes = linhas_nao_lidas = 0
    valor_em_aberto = Decimal("0.00")
    if pares:
        lanc_banco = aliased(Lancamento)
        lanc_sistema = aliased(Lancamento)
        linhas = db.execute(
            select(
                Conciliacao.extrato_banco_id,
                Conciliacao.lancamento_banco_id,
                lanc_banco.valor.label("banco_valor"),
                lanc_sistema.valor.label("sistema_valor"),
                lanc_sistema.data.label("sistema_data"),
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

        bancos = [par.extrato_banco_id for par in pares]
        eventos_por_linha: dict[tuple[uuid.UUID, str], list[DecisaoLinha]] = defaultdict(list)
        for evento in db.scalars(
            select(DecisaoLinha)
            .where(
                DecisaoLinha.empresa_id == empresa_id,
                DecisaoLinha.extrato_banco_id.in_(bancos),
            )
            .order_by(DecisaoLinha.em, DecisaoLinha.id)
        ):
            eventos_por_linha[(evento.extrato_banco_id, evento.chave)].append(evento)

        for linha in linhas:
            chave = decisoes.chave_da_linha(
                linha.lancamento_banco_id,
                linha.sistema_valor,
                linha.sistema_data,
                linha.sistema_descricao,
                linha.sistema_ocorrencia,
            )
            vigente = decisoes.decisao_vigente(
                eventos_por_linha.get((linha.extrato_banco_id, chave), [])
            )
            if vigente is not None and vigente.tipo == "justificada":
                justificadas += 1
            else:
                pendentes += 1
                valor = (
                    linha.banco_valor
                    if linha.lancamento_banco_id is not None
                    else linha.sistema_valor
                )
                valor_em_aberto += abs(valor)

        extratos = {par.extrato_banco_id for par in pares} | {
            par.extrato_sistema_id for par in pares
        }
        invalidas = Counter(
            {
                linha.extrato_id: linha.quantidade
                for linha in db.execute(
                    select(LinhaInvalida.extrato_id, func.count().label("quantidade"))
                    .where(LinhaInvalida.extrato_id.in_(list(extratos)))
                    .group_by(LinhaInvalida.extrato_id)
                )
            }
        )
        # Cada extrato conta uma vez, mesmo que esteja em mais de um par do mês.
        linhas_nao_lidas = sum(invalidas[extrato] for extrato in extratos)

    return {
        "pares": [
            {
                "extrato_banco_id": str(par.extrato_banco_id),
                "extrato_sistema_id": str(par.extrato_sistema_id),
                "execucao_id": str(par.execucao.id),
                "rodada": par.rodada,
                "nome_arquivo_banco": par.nome_arquivo_banco,
                "nome_arquivo_sistema": par.nome_arquivo_sistema,
            }
            for par in pares
        ],
        "contagens": contagens,
        "justificadas": justificadas,
        "pendentes": pendentes,
        "linhas_nao_lidas": linhas_nao_lidas,
        "valor_em_aberto": str(valor_em_aberto.quantize(Decimal("0.01"))),
    }
