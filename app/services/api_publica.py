"""Consultas da API pública (issue #135, ADR-021), só de leitura.

Regra única da `/v1` e das ferramentas do MCP (issue #136, ADR-022): as duas
chamam as funções de "consultas" no fim deste módulo e devolvem os mesmos
modelos (`ResumoV1`, `ConciliacoesV1`, `DivergenciasV1`, `FechamentosV1`).
Quem chama já conferiu que a empresa é do escopo da chave ou da conexão.

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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, get_args

from pydantic import BaseModel, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.models import (
    Conciliacao,
    DecisaoLinha,
    ExecucaoConciliacao,
    Extrato,
    Fechamento,
    Lancamento,
)
from app.services import decisoes, fechamentos

Situacao = Literal["sem_justificativa", "aguardando_aprovacao", "justificada", "conferida"]
Lado = Literal["banco", "sistema", "ambos"]
Categoria = Literal[
    "divergente_valor", "divergente_data", "sem_correspondencia", "duplicado", "tarifa_bancaria"
]
SITUACOES: tuple[str, ...] = get_args(Situacao)
CATEGORIAS_DE_DIVERGENCIA: tuple[str, ...] = get_args(Categoria)
PADRAO_COMPETENCIA = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
LIMITE_PADRAO = 50
LIMITE_MAXIMO = 200


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


def linhas_divergentes(
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


# --- modelos ---------------------------------------------------------------------------


class EmpresaV1(BaseModel):
    """Uma empresa cliente que a chave enxerga."""

    id: uuid.UUID = Field(description="Identificador da empresa, usado nas outras rotas.")
    razao_social: str = Field(description="Razão social da empresa.")
    cnpj: str = Field(description="CNPJ da empresa, sem máscara (14 caracteres).")


class EmpresasV1(BaseModel):
    itens: list[EmpresaV1] = Field(description="As empresas no escopo da chave.")


class ContagensV1(BaseModel):
    """Quantas linhas caíram em cada categoria da conciliação."""

    total: int = Field(description="Total de linhas dos pares conciliados.")
    match_exato: int = Field(description="Linhas que bateram em valor e data.")
    match_tolerancia: int = Field(
        description="Linhas que bateram em valor, com a data dentro da tolerância de dias."
    )
    duplicado: int = Field(description="Lançamentos excedentes de um grupo que já formou par.")
    sem_correspondencia: int = Field(description="Lançamentos sem nada parecido do outro lado.")
    tarifa_bancaria: int = Field(description="Lançamentos do banco reconhecidos como tarifa.")
    divergente_valor: int = Field(
        description="Há lançamento do outro lado na mesma data, com valor diferente."
    )
    divergente_data: int = Field(
        description="Há lançamento do outro lado com o mesmo valor, em outra data."
    )


class ContagensDoMesV1(ContagensV1):
    justificadas: int = Field(description="Divergências com justificativa aprovada.")
    aguardando_aprovacao: int = Field(
        description="Divergências com justificativa ainda não aprovada por admin ou gestor."
    )
    pendentes: int = Field(description="Divergências sem justificativa.")
    linhas_nao_lidas: int = Field(
        description="Linhas dos arquivos de extrato que não puderam ser lidas."
    )


class ResumoV1(BaseModel):
    """A situação de uma competência (mês) de uma empresa."""

    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    fechado: bool = Field(description="O mês está fechado (há um fechamento ativo).")
    fechado_em: datetime | None = Field(description="Quando o mês foi fechado; nulo se aberto.")
    com_ressalva: bool = Field(
        description="O fechamento ativo foi feito com ressalva. O texto da ressalva não é exposto."
    )
    pronto_para_fechar: bool = Field(
        description=(
            "O mês está aberto, tem conciliação e não tem divergência sem justificativa, "
            "justificativa aguardando aprovação nem linha não lida."
        )
    )
    contagens: ContagensDoMesV1


class ContagensDoParV1(ContagensV1):
    justificadas: int = Field(description="Divergências deste par com justificativa aprovada.")
    aguardando_aprovacao: int = Field(
        description="Divergências deste par com justificativa aguardando aprovação."
    )


class ConciliacaoV1(BaseModel):
    """Um par vigente do mês: a rodada mais recente de um extrato do banco."""

    extrato_banco_id: uuid.UUID = Field(description="Extrato do banco do par.")
    extrato_sistema_id: uuid.UUID = Field(description="Extrato do sistema (ERP) do par.")
    rodada: int = Field(
        description="Número da rodada: quantos extratos do sistema já foram conciliados com este extrato do banco."
    )
    executada_em: datetime = Field(description="Quando a conciliação deste par rodou por último.")
    periodo_inicio: date | None = Field(
        description="Primeiro dia com lançamento no extrato do banco."
    )
    periodo_fim: date | None = Field(description="Último dia com lançamento no extrato do banco.")
    contagens: ContagensDoParV1


class ConciliacoesV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    itens: list[ConciliacaoV1]


class DivergenciaV1(BaseModel):
    """Uma linha divergente de um par vigente do mês. Sem descrição do lançamento."""

    extrato_banco_id: uuid.UUID = Field(description="Extrato do banco da linha.")
    chave: str = Field(
        description="Identificador estável da linha dentro do extrato do banco, igual em todas as rodadas."
    )
    categoria: Categoria = Field(description="Tipo de divergência.")
    data: date = Field(description="Data do lançamento (o do banco ou, sem ele, o do sistema).")
    valor: Decimal = Field(
        description="Valor do lançamento, string decimal com duas casas; negativo é saída."
    )
    lado: Lado = Field(description="De que lado está o lançamento: banco, sistema ou ambos.")
    situacao: Situacao = Field(
        description=(
            "sem_justificativa: ninguém tratou; aguardando_aprovacao: justificada, falta aprovar; "
            "justificada: justificativa aprovada; conferida: marcada como conferida."
        )
    )


class DivergenciasV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    total: int = Field(description="Total de linhas que atendem aos filtros, em todas as páginas.")
    limit: int
    offset: int
    itens: list[DivergenciaV1]


class FechamentoV1(BaseModel):
    """Um fechamento de mês, ativo ou reaberto. Sem nomes e sem o texto da ressalva."""

    competencia: str = Field(description="Mês fechado, AAAA-MM.")
    fechado_em: datetime = Field(description="Quando o mês foi fechado.")
    com_ressalva: bool = Field(description="Foi fechado com ressalva.")
    reaberto_em: datetime | None = Field(
        description="Quando foi reaberto; nulo se continua fechado."
    )
    contagens: ContagensDoMesV1 = Field(description="As contagens congeladas no fechamento.")


class FechamentosV1(BaseModel):
    itens: list[FechamentoV1]


# --- consultas (a /v1 e o MCP) ---------------------------------------------------------


def _contagens_do_resumo(resumo: dict) -> ContagensDoMesV1:
    return ContagensDoMesV1(
        **resumo["contagens"],
        justificadas=resumo["justificadas"],
        aguardando_aprovacao=resumo.get("aguardando_aprovacao", 0),
        pendentes=resumo["pendentes"],
        linhas_nao_lidas=resumo["linhas_nao_lidas"],
    )


def resumo_do_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> ResumoV1:
    ativo = fechamentos.fechamento_ativo(db, empresa_id, competencia)
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    resumo = fechamentos.resumo_do_mes(db, empresa_id, pares)
    contagens = _contagens_do_resumo(resumo)
    pronto = (
        ativo is None
        and bool(pares)
        and contagens.pendentes == 0
        and contagens.aguardando_aprovacao == 0
        and contagens.linhas_nao_lidas == 0
    )
    return ResumoV1(
        competencia=competencia,
        fechado=ativo is not None,
        fechado_em=ativo.fechado_em if ativo is not None else None,
        com_ressalva=ativo is not None and ativo.ressalva is not None,
        pronto_para_fechar=pronto,
        contagens=contagens,
    )


def conciliacoes_do_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> ConciliacoesV1:
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    periodos = periodos_dos_extratos(db, empresa_id, [par.extrato_banco_id for par in pares])
    justificativas = decisoes.contar_justificativas(
        db, empresa_id, {(par.extrato_banco_id, par.extrato_sistema_id) for par in pares}
    )
    itens = []
    for par in pares:
        contagem = justificativas.get((par.extrato_banco_id, par.extrato_sistema_id))
        inicio, fim = periodos.get(par.extrato_banco_id, (None, None))
        itens.append(
            ConciliacaoV1(
                extrato_banco_id=par.extrato_banco_id,
                extrato_sistema_id=par.extrato_sistema_id,
                rodada=par.rodada,
                executada_em=par.execucao.criado_em.replace(tzinfo=UTC),
                periodo_inicio=inicio,
                periodo_fim=fim,
                contagens=ContagensDoParV1(
                    **{c: getattr(par.execucao, c) for c in fechamentos.CATEGORIAS},
                    justificadas=contagem.aprovadas if contagem else 0,
                    aguardando_aprovacao=contagem.aguardando_aprovacao if contagem else 0,
                ),
            )
        )
    return ConciliacoesV1(competencia=competencia, itens=itens)


def divergencias_do_mes(
    db: Session,
    empresa_id: uuid.UUID,
    competencia: str,
    categoria: str | None = None,
    situacao: str | None = None,
    limit: int = LIMITE_PADRAO,
    offset: int = 0,
) -> DivergenciasV1:
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    linhas = [
        d
        for d in linhas_divergentes(db, empresa_id, pares)
        if (categoria is None or d.categoria == categoria)
        and (situacao is None or d.situacao == situacao)
    ]
    return DivergenciasV1(
        competencia=competencia,
        total=len(linhas),
        limit=limit,
        offset=offset,
        itens=[DivergenciaV1(**vars(d)) for d in linhas[offset : offset + limit]],
    )


def fechamentos_da_empresa(
    db: Session, empresa_id: uuid.UUID, competencia: str | None = None
) -> FechamentosV1:
    consulta = select(Fechamento).where(Fechamento.empresa_id == empresa_id)
    if competencia is not None:
        consulta = consulta.where(Fechamento.competencia == competencia)
    itens = db.scalars(
        consulta.order_by(Fechamento.competencia.desc(), Fechamento.fechado_em.desc())
    )
    return FechamentosV1(
        itens=[
            FechamentoV1(
                competencia=f.competencia,
                fechado_em=f.fechado_em,
                com_ressalva=f.ressalva is not None,
                reaberto_em=f.reaberto_em,
                contagens=_contagens_do_resumo(f.resumo),
            )
            for f in itens
        ]
    )


class MesV1(BaseModel):
    """Uma competência com conciliação."""

    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    fechado: bool = Field(description="O mês está fechado (há um fechamento ativo).")


class MesesV1(BaseModel):
    itens: list[MesV1] = Field(description="Das competências mais recentes para as mais antigas.")


def meses_com_conciliacao(db: Session, empresa_id: uuid.UUID, limite: int = 24) -> MesesV1:
    """As competências com conciliação, pela regra do fechamento: o mês do
    `periodo_inicio` do extrato do banco. Duas consultas."""
    mes = func.to_char(Extrato.periodo_inicio, "YYYY-MM")
    competencias = list(
        db.scalars(
            select(mes)
            .join(ExecucaoConciliacao, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
            .where(
                ExecucaoConciliacao.empresa_id == empresa_id,
                Extrato.periodo_inicio.is_not(None),
            )
            .group_by(mes)
            .order_by(mes.desc())
            .limit(limite)
        )
    )
    fechados = (
        set(
            db.scalars(
                select(Fechamento.competencia).where(
                    Fechamento.empresa_id == empresa_id,
                    Fechamento.reaberto_em.is_(None),
                    Fechamento.competencia.in_(competencias),
                )
            )
        )
        if competencias
        else set()
    )
    return MesesV1(itens=[MesV1(competencia=c, fechado=c in fechados) for c in competencias])
