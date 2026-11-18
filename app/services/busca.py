"""Busca do topo do app (Ctrl K) nos lançamentos de todas as conciliações da
empresa ativa (issue #220). A rota está em app/api/busca.py.

O que entra: as linhas de `conciliacoes` do par em vigor de cada extrato do
banco da empresa, a rodada mais recente de `decisoes.rodadas_mais_recentes`.
É o mesmo par em que as decisões são gravadas e o que o front abre. Um extrato
do banco conciliado com o sistema A e depois com o B só aparece pelo par com
o B. Extrato nunca conciliado fica de fora: não há linha para abrir. O motor
apaga e regrava as linhas de um par a cada rodada (matching.py), então não há
linha velha do mesmo par.

O termo é um campo só, como a caixa do front sugere. `interpretar_termo` tenta
lê-lo de três jeitos, e a linha entra se qualquer um bater, no lançamento do
banco ou no do sistema:

- valor: "1.234,56", "1234,56", "1234.56", "1,234.56", com ou sem "R$" e
  sinal. Compara o valor absoluto: "150" acha o crédito de 150 e o débito de
  -150. "1.234" é mil duzentos e trinta e quatro (o ponto de milhar
  brasileiro), porque um valor não tem três casas decimais. "12,345" não é
  valor: seria a vírgula de milhar sem a parte decimal, ambíguo demais;
- data: "13/10" (qualquer ano), "13/10/2026", "13/10/26" e "2026-10-13".
  "13.10" e "13-10" ficam como valor ou texto;
- texto: a descrição contém o termo, sem diferenciar acento nem maiúsculas
  (app/core/sem_acento.py). Um caractere só não entra como texto, para "a"
  não achar tudo. `%` e `_` são texto comum (`autoescape`).

Com mais de uma palavra, cada palavra também é lida sozinha, dos mesmos três
jeitos, e o lançamento entra se todas baterem nele, em qualquer ordem:
"joao pix" acha "PIX JOAO", e "joao 150" acha o Pix do João de 150. As
palavras precisam estar no mesmo lançamento, não uma no banco e outra no
sistema da linha. Uma palavra de um caractere que não é valor ("e", "a") fica
de fora. O termo inteiro continua valendo, para "R$ 150" não exigir "r$" na
descrição.

Ordem: mais recentes primeiro (data do lançamento do banco, ou do sistema na
linha só do sistema), depois valor e id da conciliação, estável para paginar.
O total sai de `count(*) OVER ()`, como em GET /empresas.

Sem índice novo: a consulta parte de `conciliacoes` pelo índice de
`extrato_banco_id` e chega aos lançamentos pela chave primária, então o custo
é linear nas linhas conciliadas dos pares em vigor da empresa. Um índice
trigram (`pg_trgm`) na descrição foi testado e o Postgres não o usa nesse
formato: o termo é conferido nos dois lados da linha, com OR. Medido em
17/11/2026 (tests/test_busca_volume.py): 0,24 s com 20 mil lançamentos por
lado, num Postgres local. Se uma empresa passar de umas centenas de milhares
de linhas conciliadas, o caminho é partir dos lançamentos que batem (com o
índice trigram) e só depois ir às linhas da conciliação.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, and_, extract, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.core.sem_acento import sem_acento, sem_acento_sql
from app.models import Conciliacao, ExecucaoConciliacao, Extrato, Lancamento
from app.services import decisoes

# Valor com no máximo 12 dígitos na parte inteira: o NUMERIC(14, 2) da coluna.
_VALOR_BR_MILHAR = re.compile(r"^\d{1,3}(?:\.\d{3}){1,3}(?:,\d{1,2})?$")
_VALOR_VIRGULA = re.compile(r"^\d{1,12},\d{1,2}$")
_VALOR_PONTO = re.compile(r"^\d{1,12}(?:\.\d{1,2})?$")
_VALOR_US_MILHAR = re.compile(r"^\d{1,3}(?:,\d{3}){1,3}\.\d{1,2}$")
_PREFIXO_MOEDA = re.compile(r"^r\$\s*", re.IGNORECASE)

_DATA_BR = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$")
_DATA_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

TAMANHO_MINIMO_TEXTO = 2


@dataclass(frozen=True)
class DataBuscada:
    dia: int
    mes: int
    ano: int | None


@dataclass(frozen=True)
class TermoInterpretado:
    valores: set[Decimal] = field(default_factory=set)
    datas: list[DataBuscada] = field(default_factory=list)
    texto: str | None = None
    palavras: tuple["TermoInterpretado", ...] = ()

    @property
    def vazio(self) -> bool:
        return not self.valores and not self.datas and self.texto is None and not self.palavras


def _valor(termo: str) -> Decimal | None:
    bruto = _PREFIXO_MOEDA.sub("", termo).strip().lstrip("+-").strip()
    if _VALOR_BR_MILHAR.match(bruto):
        normalizado = bruto.replace(".", "").replace(",", ".")
    elif _VALOR_VIRGULA.match(bruto):
        normalizado = bruto.replace(",", ".")
    elif _VALOR_PONTO.match(bruto):
        normalizado = bruto
    elif _VALOR_US_MILHAR.match(bruto):
        normalizado = bruto.replace(",", "")
    else:
        return None
    try:
        return Decimal(normalizado)
    except InvalidOperation:
        return None


def _data(termo: str) -> DataBuscada | None:
    if casou := _DATA_BR.match(termo):
        dia, mes, ano = int(casou[1]), int(casou[2]), casou[3]
        ano = None if ano is None else int(ano) + (2000 if len(ano) == 2 else 0)
    elif casou := _DATA_ISO.match(termo):
        ano, mes, dia = int(casou[1]), int(casou[2]), int(casou[3])
    else:
        return None
    try:
        # Sem ano, 2024 (bissexto) só confere que o dia existe em algum ano.
        date(ano or 2024, mes, dia)
    except ValueError:
        return None
    return DataBuscada(dia=dia, mes=mes, ano=ano)


def _interpretar_sozinho(termo: str) -> TermoInterpretado:
    valor = _valor(termo)
    data = _data(termo)
    texto = sem_acento(termo) if len(termo) >= TAMANHO_MINIMO_TEXTO else None
    return TermoInterpretado(
        valores={valor} if valor is not None else set(),
        datas=[data] if data is not None else [],
        texto=texto,
    )


def interpretar_termo(termo: str) -> TermoInterpretado:
    """O termo da caixa lido como valor, data e texto, e cada palavra dele
    também (regras no docstring)."""
    termo = termo.strip()
    if not termo:
        return TermoInterpretado()
    inteiro = _interpretar_sozinho(termo)
    partes = termo.split()
    if len(partes) < 2:
        return inteiro
    palavras = tuple(palavra for palavra in map(_interpretar_sozinho, partes) if not palavra.vazio)
    return TermoInterpretado(
        valores=inteiro.valores, datas=inteiro.datas, texto=inteiro.texto, palavras=palavras
    )


def _condicao_do_lancamento(lancamento, termo: TermoInterpretado):
    condicoes = []
    if termo.palavras:
        condicoes.append(
            and_(*(_condicao_do_lancamento(lancamento, palavra) for palavra in termo.palavras))
        )
    if termo.valores:
        com_sinal = termo.valores | {-valor for valor in termo.valores}
        condicoes.append(lancamento.valor.in_(sorted(com_sinal)))
    for data in termo.datas:
        if data.ano is not None:
            condicoes.append(lancamento.data == date(data.ano, data.mes, data.dia))
        else:
            condicoes.append(
                and_(
                    extract("day", lancamento.data) == data.dia,
                    extract("month", lancamento.data) == data.mes,
                )
            )
    if termo.texto is not None:
        condicoes.append(
            sem_acento_sql(lancamento.descricao).contains(termo.texto, autoescape=True)
        )
    return or_(*condicoes)


def pares_em_vigor(db: Session, empresa_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(extrato do banco, extrato do sistema) da rodada mais recente de cada
    extrato do banco da empresa que já foi conciliado. Duas consultas."""
    extratos_banco = db.scalars(
        select(ExecucaoConciliacao.extrato_banco_id)
        .where(ExecucaoConciliacao.empresa_id == empresa_id)
        .distinct()
    ).all()
    rodadas = decisoes.rodadas_mais_recentes(db, empresa_id, list(extratos_banco))
    return sorted((banco, rodada.extrato_sistema_id) for banco, rodada in rodadas.items())


def consulta_da_busca(
    empresa_id: uuid.UUID,
    pares: list[tuple[uuid.UUID, uuid.UUID]],
    termo: TermoInterpretado,
) -> Select:
    """As linhas que batem, com o total em `total` (`count(*) OVER ()`), sem
    `limit` nem `offset`. Sempre filtra por `empresa_id` (ADR-004)."""
    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    return (
        select(
            Conciliacao.id,
            Conciliacao.extrato_banco_id,
            Conciliacao.extrato_sistema_id,
            Conciliacao.status,
            func.to_char(Extrato.periodo_inicio, "YYYY-MM").label("competencia"),
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
            func.count().over().label("total"),
        )
        .select_from(Conciliacao)
        .join(
            Extrato,
            (Extrato.id == Conciliacao.extrato_banco_id) & (Extrato.empresa_id == empresa_id),
        )
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(
            Conciliacao.empresa_id == empresa_id,
            tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(pares),
            or_(
                _condicao_do_lancamento(lanc_banco, termo),
                _condicao_do_lancamento(lanc_sistema, termo),
            ),
        )
        .order_by(
            func.coalesce(lanc_banco.data, lanc_sistema.data).desc(),
            func.coalesce(lanc_banco.valor, lanc_sistema.valor),
            Conciliacao.id,
        )
    )


def contar(db: Session, consulta: Select) -> int:
    """O total quando a página veio vazia (offset além do total): sem linha,
    não há de onde ler o `count(*) OVER ()`."""
    return db.scalar(select(func.count()).select_from(consulta.order_by(None).subquery()))
