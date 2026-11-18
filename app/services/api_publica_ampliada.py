"""Consultas ampliadas do MCP (issue #225, ADR-022), só de leitura.

Vizinho de app/services/api_publica.py e com a mesma regra: quem chama já
conferiu que a empresa é a do escopo da conexão, nada daqui devolve descrição
de lançamento, texto de justificativa, texto de ressalva, nome de pessoa nem
nome de arquivo, e o número de consultas é fixo, qualquer que seja o tamanho
do mês ou da página. Os serviços ficam fora do módulo do MCP para a `/v1`
poder reaproveitá-los depois (M3 da issue).

- `visao_do_mes`: o mês como um todo, em dinheiro (entradas, saídas, saldos,
  diferença, percentual conciliado, tarifas, duplicados, agrupamentos).
- `pendencias_do_mes`: o que falta para fechar, pela regra real do fechamento
  (app/api/fechamentos.py): justificativa aguardando aprovação impede sempre;
  divergência sem justificativa e linha não lida só impedem fechar sem
  ressalva.
- `detalhe_da_divergencia`: os dois lados, a diferença, a tolerância, a
  situação da decisão (sem o texto) e os fatos calculados no molde da
  explicação sem IA (app/services/ia/detalhes.py).
- `divergencias_com_id`: a lista de `divergencias_do_mes` com o `id` que
  `detalhe_da_divergencia` aceita e o `truncado`.
- `lancamentos_do_mes`: os lançamentos dos extratos do mês com a situação que a
  conciliação deu a cada um, filtrados, ordenados e paginados no banco.
- `comparar_meses`: a série dos últimos N meses com conciliação, por categoria
  de divergência, com a variação para o mês anterior.
- `grupos_do_mes`: os agrupamentos N para 1 e 1 para N (issue #131) com a
  composição e a soma que fechou.

O `id` de uma divergência é o da linha em `conciliacoes`. Ele vale para a
rodada vigente do mês: refazer a conciliação do par recria as linhas
(ADR-007), e o id antigo deixa de achar a divergência.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, get_args

from pydantic import BaseModel, Field
from sqlalchemy import func, literal, select, tuple_, union_all
from sqlalchemy.orm import Session, aliased

from app.models import Conciliacao, ExecucaoConciliacao, Extrato, Fechamento, Lancamento
from app.services import api_publica, decisoes, fechamentos
from app.services.api_publica import (
    LIMITE_MAXIMO,
    Categoria,
    DivergenciaV1,
    Situacao,
)
from app.services.execucoes import calcular_percentual_acerto
from app.services.ia import detalhes as ia_detalhes
from app.services.ia.contexto import montar_contexto_divergencia
from app.services.travas import competencia_do_extrato

_CENTAVOS = Decimal("0.01")
_ZERO = Decimal("0.00")


class SemConciliacao(Exception):
    """A competência não tem conciliação (nenhum extrato do banco do mês foi conciliado)."""

    def __init__(self, competencia: str) -> None:
        super().__init__(competencia)
        self.competencia = competencia


class DivergenciaNaoEncontrada(Exception):
    """Id inexistente, de outra empresa, de linha que não é divergência ou de rodada anterior.
    Os quatro casos são o mesmo, para não revelar que o id existe em outra empresa."""


def competencia_mais_recente(db: Session, empresa_id: uuid.UUID) -> str | None:
    """O mês mais recente com conciliação (a regra de `meses_com_conciliacao`), ou
    `None` se a empresa nunca conciliou. É a competência quando a ferramenta não recebe uma."""
    itens = api_publica.meses_com_conciliacao(db, empresa_id, limite=1).itens
    return itens[0].competencia if itens else None


def _dinheiro(valor: Decimal | None) -> Decimal:
    return (valor or _ZERO).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


def _percentual(parte: Decimal, total: Decimal) -> Decimal | None:
    if total == 0:
        return None
    return (parte / total * 100).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


# --- visão do mês ----------------------------------------------------------------------------


class LadoDaVisaoV1(BaseModel):
    """O que os extratos de um lado (banco ou sistema) do mês movimentaram."""

    extratos: int = Field(description="Quantos extratos deste lado entram no mês.")
    lancamentos: int = Field(description="Quantos lançamentos eles têm.")
    entradas: Decimal = Field(description="Soma dos créditos, string decimal em reais.")
    saidas: Decimal = Field(
        description="Soma dos débitos, string decimal em reais; negativa (saída tem sinal)."
    )
    liquido: Decimal = Field(description="entradas + saidas.")
    saldo_final: Decimal | None = Field(
        description=(
            "Saldo final que o arquivo do extrato informa. Nulo quando o arquivo não traz saldo "
            "e quando o lado tem mais de um extrato no mês: saldos de contas ou de períodos "
            "diferentes não se somam."
        )
    )
    saldo_data: date | None = Field(description="Data do saldo final; nulo quando ele é nulo.")


class TotalV1(BaseModel):
    quantidade: int = Field(description="Quantas linhas.")
    valor: Decimal = Field(description="Soma dos valores absolutos, string decimal em reais.")


class AgrupamentosV1(BaseModel):
    grupos: int = Field(description="Quantos agrupamentos (um lançamento contra vários).")
    lancamentos: int = Field(description="Quantos lançamentos entraram em algum grupo.")
    valor: Decimal = Field(
        description="Soma que os grupos fecharam, ao centavo, string decimal em reais."
    )


class VisaoDoMesV1(BaseModel):
    """O mês como um todo, em dinheiro e em quantidade."""

    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    fechado: bool = Field(description="O mês está fechado (há um fechamento ativo).")
    banco: LadoDaVisaoV1
    sistema: LadoDaVisaoV1
    diferenca_liquida: Decimal = Field(
        description="liquido do banco menos liquido do sistema, string decimal em reais."
    )
    saldos_batem: bool | None = Field(
        description=(
            "O saldo final do banco é igual ao do sistema. Nulo quando um dos dois não existe "
            "(veja `saldo_final` de cada lado)."
        )
    )
    diferenca_de_saldo: Decimal | None = Field(
        description="saldo final do banco menos o do sistema; nulo quando `saldos_batem` é nulo."
    )
    percentual_conciliado_quantidade: Decimal | None = Field(
        description=(
            "De 0 a 100, duas casas: linhas conciliadas (exatas, por tolerância de data e "
            "agrupadas) sobre o total de linhas. Nulo sem linhas."
        )
    )
    percentual_conciliado_valor: Decimal | None = Field(
        description=(
            "De 0 a 100, duas casas: do valor movimentado no banco (soma dos valores "
            "absolutos), quanto está em linhas conciliadas. Nulo sem movimento no banco."
        )
    )
    tarifa_bancaria: TotalV1 = Field(description="Linhas reconhecidas como tarifa bancária.")
    duplicados: TotalV1 = Field(description="Lançamentos excedentes de um grupo que já fechou par.")
    agrupamentos: AgrupamentosV1


def _saldo_do_lado(extratos: list) -> tuple[Decimal | None, date | None]:
    if len(extratos) != 1 or extratos[0].saldo_final is None:
        return None, None
    return extratos[0].saldo_final, extratos[0].saldo_data


def visao_do_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> VisaoDoMesV1:
    """Seis consultas além dos pares do mês (três): extratos, soma por extrato e tipo, e
    totais por categoria. Levanta `SemConciliacao` se o mês não tem conciliação."""
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    if not pares:
        raise SemConciliacao(competencia)
    ativo = fechamentos.fechamento_ativo(db, empresa_id, competencia)

    ids_banco = {par.extrato_banco_id for par in pares}
    ids_sistema = {par.extrato_sistema_id for par in pares}
    extratos = db.execute(
        select(Extrato.id, Extrato.saldo_final, Extrato.saldo_data).where(
            Extrato.empresa_id == empresa_id, Extrato.id.in_(ids_banco | ids_sistema)
        )
    ).all()
    por_lado = {
        "banco": [e for e in extratos if e.id in ids_banco],
        "sistema": [e for e in extratos if e.id in ids_sistema],
    }

    somas = db.execute(
        select(
            Lancamento.extrato_id,
            Lancamento.tipo,
            func.count().label("quantidade"),
            func.sum(Lancamento.valor).label("soma"),
        )
        .where(
            Lancamento.empresa_id == empresa_id,
            Lancamento.extrato_id.in_(ids_banco | ids_sistema),
        )
        .group_by(Lancamento.extrato_id, Lancamento.tipo)
    ).all()

    lados: dict[str, LadoDaVisaoV1] = {}
    for nome, ids in (("banco", ids_banco), ("sistema", ids_sistema)):
        proprias = [s for s in somas if s.extrato_id in ids]
        entradas = sum((s.soma for s in proprias if s.tipo == "credito"), _ZERO)
        saidas = sum((s.soma for s in proprias if s.tipo == "debito"), _ZERO)
        saldo, saldo_data = _saldo_do_lado(por_lado[nome])
        lados[nome] = LadoDaVisaoV1(
            extratos=len(ids),
            lancamentos=sum(s.quantidade for s in proprias),
            entradas=_dinheiro(entradas),
            saidas=_dinheiro(saidas),
            liquido=_dinheiro(entradas + saidas),
            saldo_final=saldo,
            saldo_data=saldo_data,
        )

    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    por_status = {
        linha.status: linha
        for linha in db.execute(
            select(
                Conciliacao.status,
                func.count().label("quantidade"),
                func.coalesce(
                    func.sum(func.abs(func.coalesce(lanc_banco.valor, lanc_sistema.valor))), 0
                ).label("valor"),
                func.coalesce(func.sum(func.abs(lanc_banco.valor)), 0).label("valor_banco"),
            )
            .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
            .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
            .where(
                Conciliacao.empresa_id == empresa_id,
                tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(
                    [(par.extrato_banco_id, par.extrato_sistema_id) for par in pares]
                ),
            )
            .group_by(Conciliacao.status)
        )
    }

    def total(status: str) -> TotalV1:
        linha = por_status.get(status)
        return TotalV1(
            quantidade=linha.quantidade if linha else 0,
            valor=_dinheiro(linha.valor if linha else None),
        )

    contagens = {
        categoria: sum(getattr(par.execucao, categoria) for par in pares)
        for categoria in ("total", "match_exato", "match_tolerancia", "match_agrupado")
    }
    grupos = sum(par.execucao.agrupamentos for par in pares)
    valor_banco_total = sum((linha.valor_banco for linha in por_status.values()), _ZERO)
    valor_banco_conciliado = sum(
        (linha.valor_banco for status, linha in por_status.items() if not decisoes.diverge(status)),
        _ZERO,
    )

    banco, sistema = lados["banco"], lados["sistema"]
    saldos = (
        (banco.saldo_final, sistema.saldo_final)
        if banco.saldo_final is not None and sistema.saldo_final is not None
        else None
    )
    percentual_quantidade = calcular_percentual_acerto(
        contagens["match_exato"],
        contagens["match_tolerancia"],
        contagens["total"],
        contagens["match_agrupado"],
    )
    return VisaoDoMesV1(
        competencia=competencia,
        fechado=ativo is not None,
        banco=banco,
        sistema=sistema,
        diferenca_liquida=_dinheiro(banco.liquido - sistema.liquido),
        saldos_batem=saldos[0] == saldos[1] if saldos else None,
        diferenca_de_saldo=_dinheiro(saldos[0] - saldos[1]) if saldos else None,
        percentual_conciliado_quantidade=percentual_quantidade,
        percentual_conciliado_valor=_percentual(valor_banco_conciliado, valor_banco_total),
        tarifa_bancaria=total("tarifa_bancaria"),
        duplicados=total("duplicado"),
        agrupamentos=AgrupamentosV1(
            grupos=grupos,
            lancamentos=contagens["match_agrupado"],
            valor=_dinheiro(
                por_status["match_agrupado"].valor_banco if "match_agrupado" in por_status else None
            ),
        ),
    )


# --- divergências com id ---------------------------------------------------------------------


class DivergenciaComIdV1(DivergenciaV1):
    """A divergência da `/v1` mais o `id` que `detalhe_da_divergencia` aceita."""

    id: uuid.UUID = Field(
        description="Identificador da divergência; vale para `detalhe_da_divergencia`."
    )


class DivergenciasComIdV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    total: int = Field(description="Total de linhas que atendem aos filtros, em todas as páginas.")
    limit: int = Field(description="Itens por página pedidos.")
    offset: int = Field(description="Quantos itens foram pulados.")
    truncado: bool = Field(description="Há mais linhas além das desta página.")
    itens: list[DivergenciaComIdV1]


def divergencias_com_id(
    db: Session,
    empresa_id: uuid.UUID,
    competencia: str,
    *,
    categoria: str | None = None,
    situacao: str | None = None,
    valor_minimo: Decimal | None = None,
    valor_maximo: Decimal | None = None,
    ordenar_por: api_publica.Ordenar = "data",
    ordem: api_publica.Ordem = "asc",
    limit: int = api_publica.LIMITE_PADRAO,
    offset: int = 0,
) -> DivergenciasComIdV1:
    linhas = api_publica.listar_divergencias(
        db,
        empresa_id,
        competencia,
        categoria,
        situacao,
        valor_minimo,
        valor_maximo,
        ordenar_por,
        ordem,
    )
    pagina = linhas[offset : offset + limit]
    return DivergenciasComIdV1(
        competencia=competencia,
        total=len(linhas),
        limit=limit,
        offset=offset,
        truncado=len(linhas) > offset + len(pagina),
        itens=[
            DivergenciaComIdV1(**api_publica.divergencia_v1(d).model_dump(), id=d.conciliacao_id)
            for d in pagina
        ],
    )


# --- pendências do mês -----------------------------------------------------------------------

Impede = Literal["sempre", "sem_ressalva"]


class IdsV1(BaseModel):
    total: int = Field(description="Quantas divergências.")
    ids: list[uuid.UUID] = Field(
        description=(
            f"Ids para `detalhe_da_divergencia`, das de maior valor para as de menor, no máximo "
            f"{LIMITE_MAXIMO}."
        )
    )
    truncado: bool = Field(description="`total` passa de quantos ids vieram.")


class MotivoV1(BaseModel):
    codigo: Literal[
        "mes_fechado",
        "sem_conciliacao",
        "justificativas_aguardando_aprovacao",
        "divergencias_sem_justificativa",
        "linhas_nao_lidas",
    ] = Field(description="Código fixo do motivo.")
    descricao: str = Field(description="O motivo em português.")
    quantidade: int = Field(description="Quantas ocorrências; 0 nos motivos do mês como um todo.")
    impede: Impede = Field(
        description=(
            "sempre: não fecha nem com ressalva; sem_ressalva: só fecha com ressalva. "
            "O mês já fechado ou sem conciliação não fecha de jeito nenhum (sempre)."
        )
    )


class PendenciasV1(BaseModel):
    """O que falta para fechar o mês, pela regra do fechamento."""

    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    fechado: bool = Field(description="O mês já está fechado.")
    pode_fechar: bool = Field(
        description="Pode fechar sem ressalva: aberto, com conciliação e sem nenhum motivo abaixo."
    )
    pode_fechar_com_ressalva: bool = Field(
        description="Pode fechar com ressalva: só há motivos que impedem `sem_ressalva`."
    )
    motivos: list[MotivoV1] = Field(description="O que impede; vazio quando `pode_fechar`.")
    sem_justificativa: IdsV1 = Field(
        description=(
            "Divergências sem justificativa, inclusive as só conferidas (conferir não "
            "justifica, e conta como pendente no fechamento)."
        )
    )
    aguardando_aprovacao: IdsV1 = Field(
        description="Divergências com justificativa que admin ou gestor ainda não aprovou."
    )
    linhas_nao_lidas: int = Field(
        description="Linhas dos arquivos de extrato que não puderam ser lidas."
    )


def _ids(linhas: list[api_publica.Divergencia]) -> IdsV1:
    maiores = sorted(linhas, key=lambda d: (-abs(d.valor), d.data, str(d.conciliacao_id)))
    return IdsV1(
        total=len(linhas),
        ids=[d.conciliacao_id for d in maiores[:LIMITE_MAXIMO]],
        truncado=len(linhas) > LIMITE_MAXIMO,
    )


def pendencias_do_mes(db: Session, empresa_id: uuid.UUID, competencia: str) -> PendenciasV1:
    """Os números vêm de `fechamentos.resumo_do_mes`, a mesma conta do app e do `POST
    /fechamentos`; os ids, de `linhas_divergentes`. Levanta `SemConciliacao` sem conciliação."""
    ativo = fechamentos.fechamento_ativo(db, empresa_id, competencia)
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    if not pares:
        raise SemConciliacao(competencia)
    resumo = fechamentos.resumo_do_mes(db, empresa_id, pares)
    linhas = api_publica.linhas_divergentes(db, empresa_id, pares)
    sem_justificativa = [d for d in linhas if d.situacao in ("sem_justificativa", "conferida")]
    aguardando = [d for d in linhas if d.situacao == "aguardando_aprovacao"]

    motivos: list[MotivoV1] = []
    if ativo is not None:
        motivos.append(
            MotivoV1(
                codigo="mes_fechado",
                descricao="O mês já está fechado. Reabra-o no Ledgr para alterar.",
                quantidade=0,
                impede="sempre",
            )
        )
    if resumo["aguardando_aprovacao"]:
        motivos.append(
            MotivoV1(
                codigo="justificativas_aguardando_aprovacao",
                descricao=(
                    "Há justificativas aguardando aprovação de admin ou gestor. "
                    "O mês não fecha enquanto elas não forem aprovadas ou rejeitadas."
                ),
                quantidade=resumo["aguardando_aprovacao"],
                impede="sempre",
            )
        )
    if resumo["pendentes"]:
        motivos.append(
            MotivoV1(
                codigo="divergencias_sem_justificativa",
                descricao="Há divergências sem justificativa. Justifique ou feche com ressalva.",
                quantidade=resumo["pendentes"],
                impede="sem_ressalva",
            )
        )
    if resumo["linhas_nao_lidas"]:
        motivos.append(
            MotivoV1(
                codigo="linhas_nao_lidas",
                descricao="Há linhas de extrato que não puderam ser lidas. Corrija ou feche com ressalva.",
                quantidade=resumo["linhas_nao_lidas"],
                impede="sem_ressalva",
            )
        )
    return PendenciasV1(
        competencia=competencia,
        fechado=ativo is not None,
        pode_fechar=not motivos,
        pode_fechar_com_ressalva=ativo is None and not resumo["aguardando_aprovacao"],
        motivos=motivos,
        sem_justificativa=_ids(sem_justificativa),
        aguardando_aprovacao=_ids(aguardando),
        linhas_nao_lidas=resumo["linhas_nao_lidas"],
    )


# --- detalhe da divergência ------------------------------------------------------------------


class PontaV1(BaseModel):
    """Um lançamento de um lado da divergência. Sem descrição."""

    lado: Literal["banco", "sistema"]
    data: date = Field(description="Data do lançamento, ISO 8601.")
    valor: Decimal = Field(description="Valor, string decimal em reais; negativo é saída.")
    tipo: Literal["credito", "debito"]


class DetalheV1(BaseModel):
    rotulo: str
    valor: str = Field(description="Já formatado em pt-BR (R$, dd/mm/aaaa).")


class DetalheDaDivergenciaV1(BaseModel):
    """Uma divergência explicada com fatos calculados, sem IA e sem descrição."""

    id: uuid.UUID = Field(
        description="Identificador da divergência, o mesmo de `divergencias_do_mes`."
    )
    competencia: str = Field(description="Mês da divergência, AAAA-MM.")
    categoria: Categoria
    situacao: Situacao = Field(
        description="Situação da decisão em vigor. O texto da justificativa nunca vem."
    )
    lancamento: PontaV1 = Field(
        description="O lançamento da linha (o do banco quando a linha tem os dois lados)."
    )
    contraparte: PontaV1 | None = Field(
        description=(
            "O lançamento do outro lado: a ponta gravada pelo motor (`contraparte_gravada` "
            "verdadeiro) ou o candidato mais próximo calculado agora. Nulo quando não há."
        )
    )
    contraparte_gravada: bool | None = Field(
        description="Verdadeiro se `contraparte` é o par gravado pelo motor; nulo sem contraparte."
    )
    outros_candidatos: int = Field(
        description="Quantos outros candidatos do outro lado além de `contraparte`."
    )
    diferenca_de_valor: Decimal | None = Field(
        description="valor do sistema menos o do banco; nulo sem contraparte."
    )
    diferenca_de_dias: int | None = Field(
        description="data do sistema menos a do banco, em dias (positivo: sistema depois)."
    )
    tolerancia_dias: int | None = Field(
        description="Tolerância de dias aplicada na rodada vigente do par."
    )
    detalhes: list[DetalheV1] = Field(description="Os fatos já formatados em pt-BR.")
    explicacao: str = Field(description="Explicação em português com os números, sem IA.")


def detalhe_da_divergencia(
    db: Session, empresa_id: uuid.UUID, divergencia_id: uuid.UUID
) -> DetalheDaDivergenciaV1:
    """Levanta `DivergenciaNaoEncontrada` se o id não é de uma divergência da rodada vigente
    desta empresa. Consultas fixas: a linha, o extrato do banco, a rodada, o contexto
    (de `montar_contexto_divergencia`), a ponta do sistema e os eventos da decisão."""
    linha = db.scalar(
        select(Conciliacao).where(
            Conciliacao.id == divergencia_id, Conciliacao.empresa_id == empresa_id
        )
    )
    if linha is None or not decisoes.diverge(linha.status):
        raise DivergenciaNaoEncontrada
    extrato = db.scalar(
        select(Extrato).where(
            Extrato.id == linha.extrato_banco_id, Extrato.empresa_id == empresa_id
        )
    )
    competencia = competencia_do_extrato(extrato) if extrato is not None else None
    rodada = decisoes.rodada_mais_recente(db, empresa_id, linha.extrato_banco_id)
    if (
        competencia is None
        or rodada is None
        or rodada.extrato_sistema_id != linha.extrato_sistema_id
    ):
        raise DivergenciaNaoEncontrada

    contexto = montar_contexto_divergencia(db, linha)
    lado = contexto.lado_do_item or "banco"
    outro = "sistema" if lado == "banco" else "banco"
    item = contexto.lancamento
    candidato = contexto.candidatos[0] if contexto.candidatos else None
    contraparte = candidato.lancamento if candidato else None

    banco, sistema = (item, contraparte) if lado == "banco" else (contraparte, item)
    com_diferenca = banco is not None and sistema is not None

    if linha.lancamento_banco_id is not None:
        chave = decisoes.chave_da_linha(linha.lancamento_banco_id, None, None, None, None)
    else:
        ponta = db.execute(
            select(
                Lancamento.valor, Lancamento.data, Lancamento.descricao, Lancamento.ocorrencia
            ).where(
                Lancamento.id == linha.lancamento_sistema_id,
                Lancamento.empresa_id == empresa_id,
            )
        ).one()
        chave = decisoes.chave_da_linha(
            None, ponta.valor, ponta.data, ponta.descricao, ponta.ocorrencia
        )
    eventos = decisoes.carregar_eventos(db, empresa_id, linha.extrato_banco_id, {chave})
    situacao = api_publica.situacao_da_linha(decisoes.decisao_vigente(eventos.get(chave, [])))

    return DetalheDaDivergenciaV1(
        id=linha.id,
        competencia=competencia,
        categoria=linha.status,
        situacao=situacao,
        lancamento=PontaV1(lado=lado, data=item.data, valor=item.valor, tipo=item.tipo),
        contraparte=(
            PontaV1(
                lado=outro, data=contraparte.data, valor=contraparte.valor, tipo=contraparte.tipo
            )
            if contraparte
            else None
        ),
        contraparte_gravada=contexto.par_gravado if contraparte else None,
        outros_candidatos=max(0, (contexto.total_candidatos or 0) - 1) if contraparte else 0,
        diferenca_de_valor=_dinheiro(sistema.valor - banco.valor) if com_diferenca else None,
        diferenca_de_dias=(sistema.data - banco.data).days if com_diferenca else None,
        tolerancia_dias=contexto.tolerancia_dias,
        detalhes=[
            DetalheV1(rotulo=d.rotulo, valor=d.valor) for d in ia_detalhes.montar_detalhes(contexto)
        ],
        explicacao=ia_detalhes.texto_deterministico(contexto),
    )


# --- lançamentos do mês ----------------------------------------------------------------------

SituacaoDoLancamento = Literal[
    "conciliado",
    "divergente_valor",
    "divergente_data",
    "sem_correspondencia",
    "duplicado",
    "tarifa_bancaria",
]
SITUACOES_DO_LANCAMENTO: tuple[str, ...] = get_args(SituacaoDoLancamento)
LadoDoLancamento = Literal["banco", "sistema"]
TipoDoLancamento = Literal["credito", "debito"]


class LancamentoV1(BaseModel):
    """Um lançamento do mês e como a conciliação o tratou. Sem descrição."""

    id: uuid.UUID = Field(description="Identificador do lançamento.")
    lado: LadoDoLancamento = Field(description="De qual extrato vem: banco ou sistema (ERP).")
    data: date = Field(description="Data do lançamento, ISO 8601.")
    valor: Decimal = Field(description="Valor, string decimal em reais; negativo é saída.")
    tipo: TipoDoLancamento = Field(description="credito (entrada) ou debito (saída).")
    situacao: SituacaoDoLancamento = Field(
        description=(
            "conciliado (par exato, por tolerância de data ou agrupado) ou a categoria da "
            "divergência em que ele caiu."
        )
    )
    divergencia_id: uuid.UUID | None = Field(
        description="Se é divergência, o `id` para detalhe_da_divergencia; nulo se conciliado."
    )


class LancamentosV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    total: int = Field(description="Total de lançamentos que atendem aos filtros.")
    limit: int = Field(description="Itens por página pedidos.")
    offset: int = Field(description="Quantos itens foram pulados.")
    truncado: bool = Field(description="Há mais lançamentos além dos desta página.")
    itens: list[LancamentoV1]


def lancamentos_do_mes(
    db: Session,
    empresa_id: uuid.UUID,
    competencia: str,
    *,
    lado: str | None = None,
    tipo: str | None = None,
    situacao: str | None = None,
    valor_minimo: Decimal | None = None,
    valor_maximo: Decimal | None = None,
    data_inicial: date | None = None,
    data_final: date | None = None,
    ordenar_por: api_publica.Ordenar = "data",
    ordem: api_publica.Ordem = "asc",
    limit: int = api_publica.LIMITE_PADRAO,
    offset: int = 0,
) -> LancamentosV1:
    """Os lançamentos dos extratos do mês (os do banco da competência e os do sistema dos
    pares vigentes), com a situação que a conciliação deu a cada um. Filtra, ordena e pagina
    no banco: duas consultas além dos pares, qualquer que seja o tamanho do mês. Um
    lançamento do sistema que esteja em dois pares do mês (dois extratos do banco na mesma
    competência com o mesmo extrato do sistema) aparece uma vez por par."""
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    if not pares:
        return LancamentosV1(
            competencia=competencia, total=0, limit=limit, offset=offset, truncado=False, itens=[]
        )
    # As linhas dos pares vigentes primeiro, materializadas, e só depois os lançamentos, um a um
    # pela chave primária e sem outro predicado (a empresa já está na linha do par, e os
    # lançamentos dela são da mesma empresa). Com a estatística velha de uma carga recém-feita,
    # um predicado a mais em `lancamentos` (empresa ou extrato) parece barato ao planejador, que
    # varre todos os lançamentos da empresa para cada linha do par (42 s a 62 s com 10 mil
    # lançamentos por lado, medido no teste de volume).
    linhas_do_par = (
        select(
            Conciliacao.id,
            Conciliacao.status,
            Conciliacao.lancamento_banco_id,
            Conciliacao.lancamento_sistema_id,
        )
        .where(
            Conciliacao.empresa_id == empresa_id,
            tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(
                [(par.extrato_banco_id, par.extrato_sistema_id) for par in pares]
            ),
        )
        .cte("linhas_do_par")
        .prefix_with("MATERIALIZED")
    )

    def do_lado(nome: str, coluna):
        return (
            select(
                Lancamento.id.label("id"),
                literal(nome).label("lado"),
                Lancamento.data.label("data"),
                Lancamento.valor.label("valor"),
                Lancamento.tipo.label("tipo"),
                linhas_do_par.c.status.label("status"),
                linhas_do_par.c.id.label("conciliacao_id"),
            )
            .select_from(linhas_do_par)
            .join(Lancamento, Lancamento.id == coluna)
        )

    base = union_all(
        do_lado("banco", linhas_do_par.c.lancamento_banco_id),
        do_lado("sistema", linhas_do_par.c.lancamento_sistema_id),
    ).subquery()
    c = base.c
    filtros = []
    if lado is not None:
        filtros.append(c.lado == lado)
    if tipo is not None:
        filtros.append(c.tipo == tipo)
    if situacao == "conciliado":
        filtros.append(c.status.in_(sorted(decisoes.STATUS_RESOLVIDOS)))
    elif situacao is not None:
        filtros.append(c.status == situacao)
    if valor_minimo is not None:
        filtros.append(func.abs(c.valor) >= valor_minimo)
    if valor_maximo is not None:
        filtros.append(func.abs(c.valor) <= valor_maximo)
    if data_inicial is not None:
        filtros.append(c.data >= data_inicial)
    if data_final is not None:
        filtros.append(c.data <= data_final)

    total = db.scalar(select(func.count()).select_from(base).where(*filtros)) or 0
    chaves = (
        (func.abs(c.valor), c.data, c.id) if ordenar_por == "valor" else (c.data, c.valor, c.id)
    )
    if ordem == "desc":
        chaves = tuple(chave.desc() for chave in chaves)
    linhas = db.execute(
        select(base).where(*filtros).order_by(*chaves).limit(limit).offset(offset)
    ).all()
    return LancamentosV1(
        competencia=competencia,
        total=total,
        limit=limit,
        offset=offset,
        truncado=total > offset + len(linhas),
        itens=[
            LancamentoV1(
                id=linha.id,
                lado=linha.lado,
                data=linha.data,
                valor=linha.valor,
                tipo=linha.tipo,
                situacao=("conciliado" if not decisoes.diverge(linha.status) else linha.status),
                divergencia_id=linha.conciliacao_id if decisoes.diverge(linha.status) else None,
            )
            for linha in linhas
        ],
    )


# --- comparar meses --------------------------------------------------------------------------

QUANTIDADE_PADRAO_DE_MESES = 6
QUANTIDADE_MAXIMA_DE_MESES = 12


class ContagensDeDivergenciaV1(BaseModel):
    """Linhas divergentes por categoria. Na `variacao`, a diferença para o mês anterior."""

    total: int = Field(description="Soma das cinco categorias.")
    divergente_valor: int
    divergente_data: int
    sem_correspondencia: int
    duplicado: int
    tarifa_bancaria: int


class MesComparadoV1(BaseModel):
    competencia: str = Field(description="Mês, AAAA-MM.")
    fechado: bool = Field(description="O mês está fechado.")
    linhas: int = Field(description="Total de linhas dos pares conciliados do mês.")
    conciliadas: int = Field(
        description="Linhas conciliadas (exatas, por tolerância de data e agrupadas)."
    )
    divergencias: ContagensDeDivergenciaV1
    variacao: ContagensDeDivergenciaV1 | None = Field(
        description="Este mês menos o mês anterior da série; nulo no primeiro da série."
    )
    variacao_percentual: Decimal | None = Field(
        description=(
            "Variação do total de divergências sobre o do mês anterior, em %, duas casas; "
            "nulo no primeiro da série e quando o mês anterior não tinha divergência."
        )
    )


class ComparacaoV1(BaseModel):
    quantidade: int = Field(description="Quantos meses vieram.")
    itens: list[MesComparadoV1] = Field(
        description="Do mês mais antigo para o mais recente, só meses com conciliação."
    )


_CATEGORIAS_DIVERGENTES = (
    "divergente_valor",
    "divergente_data",
    "sem_correspondencia",
    "duplicado",
    "tarifa_bancaria",
)


def _divergencias_do_mes(pares: list[fechamentos.ParDoMes]) -> ContagensDeDivergenciaV1:
    contagens = {c: sum(getattr(par.execucao, c) for par in pares) for c in _CATEGORIAS_DIVERGENTES}
    return ContagensDeDivergenciaV1(total=sum(contagens.values()), **contagens)


def comparar_meses(
    db: Session,
    empresa_id: uuid.UUID,
    quantidade: int = QUANTIDADE_PADRAO_DE_MESES,
    ate: str | None = None,
) -> ComparacaoV1:
    """Os `quantidade` meses com conciliação mais recentes até `ate` (inclusive), do mais
    antigo para o mais recente, cada um com as divergências por categoria e a variação para o
    mês anterior da série. Número fixo de consultas, qualquer que seja `quantidade`."""
    mes = func.to_char(Extrato.periodo_inicio, "YYYY-MM")
    consulta = (
        select(mes)
        .join(ExecucaoConciliacao, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
        .where(ExecucaoConciliacao.empresa_id == empresa_id, Extrato.periodo_inicio.is_not(None))
    )
    if ate is not None:
        consulta = consulta.where(mes <= ate)
    competencias = sorted(db.scalars(consulta.group_by(mes).order_by(mes.desc()).limit(quantidade)))
    if not competencias:
        return ComparacaoV1(quantidade=0, itens=[])
    pares = fechamentos.pares_dos_meses(db, empresa_id, competencias)
    fechados = set(
        db.scalars(
            select(Fechamento.competencia).where(
                Fechamento.empresa_id == empresa_id,
                Fechamento.reaberto_em.is_(None),
                Fechamento.competencia.in_(competencias),
            )
        )
    )
    itens: list[MesComparadoV1] = []
    anterior: ContagensDeDivergenciaV1 | None = None
    for competencia in competencias:
        do_mes = pares.get(competencia, [])
        divergencias = _divergencias_do_mes(do_mes)
        variacao = (
            ContagensDeDivergenciaV1(
                **{
                    campo: getattr(divergencias, campo) - getattr(anterior, campo)
                    for campo in ("total", *_CATEGORIAS_DIVERGENTES)
                }
            )
            if anterior is not None
            else None
        )
        itens.append(
            MesComparadoV1(
                competencia=competencia,
                fechado=competencia in fechados,
                linhas=sum(par.execucao.total for par in do_mes),
                conciliadas=sum(
                    par.execucao.match_exato
                    + par.execucao.match_tolerancia
                    + par.execucao.match_agrupado
                    for par in do_mes
                ),
                divergencias=divergencias,
                variacao=variacao,
                variacao_percentual=(
                    _percentual(Decimal(variacao.total), Decimal(anterior.total))
                    if variacao is not None and anterior is not None
                    else None
                ),
            )
        )
        anterior = divergencias
    return ComparacaoV1(quantidade=len(itens), itens=itens)


# --- grupos do mês ---------------------------------------------------------------------------

GRUPOS_LIMITE_PADRAO = 10
GRUPOS_LIMITE_MAXIMO = 25
MEMBROS_POR_GRUPO = 50


class MembroDoGrupoV1(BaseModel):
    id: uuid.UUID = Field(description="Identificador do lançamento.")
    data: date = Field(description="Data do lançamento, ISO 8601.")
    valor: Decimal = Field(description="Valor, string decimal em reais; negativo é saída.")


class GrupoV1(BaseModel):
    """Um agrupamento: um lançamento de um lado cuja soma bate, ao centavo, com a de vários
    do outro (pagamento em lote)."""

    grupo_id: uuid.UUID = Field(description="Identificador do grupo.")
    lado_unico: LadoDoLancamento = Field(
        description="Lado do lançamento único: banco (um do banco contra vários do sistema) ou sistema."
    )
    soma: Decimal = Field(description="A soma que fechou, ao centavo, string decimal em reais.")
    unico: MembroDoGrupoV1 = Field(description="O lançamento único.")
    quantidade_do_outro_lado: int = Field(description="Quantos lançamentos há do outro lado.")
    do_outro_lado: list[MembroDoGrupoV1] = Field(
        description=f"Os lançamentos do outro lado, por data, no máximo {MEMBROS_POR_GRUPO}."
    )
    truncado: bool = Field(description="`quantidade_do_outro_lado` passa dos listados.")


class GruposV1(BaseModel):
    competencia: str = Field(description="Mês de referência, AAAA-MM.")
    total: int = Field(description="Quantos grupos o mês tem.")
    limit: int = Field(description="Grupos por página pedidos.")
    offset: int = Field(description="Quantos grupos foram pulados.")
    truncado: bool = Field(description="Há mais grupos além dos desta página.")
    itens: list[GrupoV1] = Field(description="Dos de maior soma para os de menor.")


def grupos_do_mes(
    db: Session,
    empresa_id: uuid.UUID,
    competencia: str,
    limit: int = GRUPOS_LIMITE_PADRAO,
    offset: int = 0,
) -> GruposV1:
    """Os agrupamentos N para 1 e 1 para N do mês (issue #131, ADR-027). Três consultas além
    dos pares: o total, a página de grupos e os lançamentos dos grupos da página."""
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    vazio = GruposV1(
        competencia=competencia, total=0, limit=limit, offset=offset, truncado=False, itens=[]
    )
    if not pares:
        return vazio
    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    do_mes = (
        Conciliacao.empresa_id == empresa_id,
        tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(
            [(par.extrato_banco_id, par.extrato_sistema_id) for par in pares]
        ),
        Conciliacao.status == "match_agrupado",
    )
    # Cada linha do grupo guarda uma ponta; a soma do lado do banco é a do grupo inteiro.
    soma_do_grupo = func.sum(func.abs(func.coalesce(lanc_banco.valor, 0)))
    grupos = (
        select(Conciliacao.grupo_id.label("grupo_id"), soma_do_grupo.label("soma"))
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .where(*do_mes)
        .group_by(Conciliacao.grupo_id)
    )
    total = db.scalar(select(func.count()).select_from(grupos.subquery())) or 0
    pagina = db.execute(
        grupos.order_by(soma_do_grupo.desc(), Conciliacao.grupo_id).limit(limit).offset(offset)
    ).all()
    if not pagina:
        return vazio.model_copy(update={"total": total})

    linhas = db.execute(
        select(
            Conciliacao.grupo_id,
            Conciliacao.lancamento_banco_id,
            func.coalesce(lanc_banco.id, lanc_sistema.id).label("lancamento_id"),
            func.coalesce(lanc_banco.data, lanc_sistema.data).label("data"),
            func.coalesce(lanc_banco.valor, lanc_sistema.valor).label("valor"),
        )
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(*do_mes, Conciliacao.grupo_id.in_([g.grupo_id for g in pagina]))
        .order_by(
            Conciliacao.grupo_id,
            func.coalesce(lanc_banco.data, lanc_sistema.data),
            func.coalesce(lanc_banco.valor, lanc_sistema.valor),
            func.coalesce(lanc_banco.id, lanc_sistema.id),
        )
    ).all()
    por_grupo: dict[uuid.UUID, list] = defaultdict(list)
    for linha in linhas:
        por_grupo[linha.grupo_id].append(linha)

    itens = []
    for grupo in pagina:
        membros = por_grupo[grupo.grupo_id]
        do_banco = [m for m in membros if m.lancamento_banco_id is not None]
        do_sistema = [m for m in membros if m.lancamento_banco_id is None]
        # O lado com um lançamento só é o único; o outro tem vários.
        unico_lado, unicos, outros = (
            ("banco", do_banco, do_sistema)
            if len(do_banco) == 1 and len(do_sistema) != 1
            else ("sistema", do_sistema, do_banco)
        )
        unico = unicos[0]
        itens.append(
            GrupoV1(
                grupo_id=grupo.grupo_id,
                lado_unico=unico_lado,
                soma=_dinheiro(grupo.soma),
                unico=MembroDoGrupoV1(id=unico.lancamento_id, data=unico.data, valor=unico.valor),
                quantidade_do_outro_lado=len(outros),
                do_outro_lado=[
                    MembroDoGrupoV1(id=m.lancamento_id, data=m.data, valor=m.valor)
                    for m in outros[:MEMBROS_POR_GRUPO]
                ],
                truncado=len(outros) > MEMBROS_POR_GRUPO,
            )
        )
    return GruposV1(
        competencia=competencia,
        total=total,
        limit=limit,
        offset=offset,
        truncado=total > offset + len(itens),
        itens=itens,
    )
