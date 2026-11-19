"""As consultas do assistente com dados (issue #223, ADR-028).

O modelo só escolhe a consulta (`ConsultaDeDados`); aqui o backend busca no
banco da empresa ativa e escreve a resposta com textos prontos. O resultado
nunca volta ao provedor.

Regras dos textos:

- só números, datas, meses, situações, categorias e a razão social das
  empresas da conta. Nunca outro texto livre do banco (descrição de
  lançamento, justificativa, ressalva, nome de arquivo ou de pessoa): a
  resposta volta no histórico da próxima pergunta, e o que não está nela não
  tem como ir ao provedor. A razão social também volta, mas a rota a troca
  por `[EMPRESA]` antes de a conversa sair (`marcar_conversa`, `nomes`);
- reais em pt-BR ("R$ 1.234,56"), datas em dd/mm/aaaa, meses por extenso
  ("setembro/2026");
- saldo que o arquivo não informa é "não informa o saldo", nunca R$ 0,00.

As consultas reaproveitam as funções da `/v1` e do MCP (app/services/
api_publica.py), as mesmas regras de mês (o `periodo_inicio` do extrato do
banco) e de par vigente. "Em aberto" segue a Visão geral do front
(`valorEmAberto`): as linhas que não bateram e não têm justificativa
aprovada; com os dois lados, a diferença; com um, o valor.

Só leitura: a rota chama `responder` dentro de `executar_somente_leitura`,
uma transação `READ ONLY` do Postgres. Uma escrita ali é recusada pelo banco,
não só pela ausência de código que grave (ADR-028: o assistente nunca grava).
"""

import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, TypeVar
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Empresa, ExecucaoConciliacao, Extrato, Fechamento
from app.services import api_publica, fechamentos
from app.services.ia.base import ConsultaDeDados
from app.services.saldo import saldo_do_extrato

# Até quantos meses atrás o assistente consulta, além do mês atual.
JANELA_MESES = 12
MAXIMO_MESES_NO_TEXTO = 12
MAXIMO_LINKS_DE_CONCILIACAO = 3
MAXIMO_EMPRESAS_POR_GRUPO = 10

_PADRAO_COMPETENCIA = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_MESES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
# Categoria de divergência: (singular, plural).
_CATEGORIAS = {
    "divergente_valor": ("divergência de valor", "divergências de valor"),
    "divergente_data": ("divergência de data", "divergências de data"),
    "sem_correspondencia": ("lançamento sem correspondência", "lançamentos sem correspondência"),
    "duplicado": ("lançamento duplicado", "lançamentos duplicados"),
    "tarifa_bancaria": ("tarifa bancária", "tarifas bancárias"),
}
_RESOLVIDOS = ("match_exato", "match_tolerancia", "match_agrupado")

T = TypeVar("T")


@dataclass(frozen=True)
class LinkDeTela:
    """Caminho de uma tela do app, sem o prefixo `/e/<empresa>`, que o front
    acrescenta. Sempre montado aqui, de uma lista fechada de telas.

    `tipo` "ajuda" é a exceção para o que não tem tela própria no front (as
    Configurações são uma janela, sem rota): o caminho é a seção da Central
    que ensina a chegar lá, e o front não acrescenta o prefixo."""

    titulo: str
    caminho: str
    tipo: Literal["tela", "ajuda"] = "tela"


@dataclass(frozen=True)
class RespostaDeDados:
    texto: str
    links: tuple[LinkDeTela, ...]


@dataclass(frozen=True)
class Recusa:
    """O mês pedido não dá para consultar; `texto` diz por quê."""

    texto: str


FECHAMENTOS = LinkDeTela("Fechamentos", "/fechamentos")
HISTORICO = LinkDeTela("Histórico", "/historico")
# Onde a pessoa faz o que pediu ao assistente, que só consulta (ADR-028).
TELAS_DE_ACAO = {
    "extratos": LinkDeTela("Extratos", "/extratos"),
    "nova_conciliacao": LinkDeTela("Nova conciliação", "/conciliacoes/nova"),
    "conciliacoes": HISTORICO,
    "fechamentos": FECHAMENTOS,
    # a regra da conciliação (tolerância de data) fica na janela Configurações
    "regras": LinkDeTela("Configurações", "/ajuda/configuracoes#tolerancia", "ajuda"),
    "empresas": LinkDeTela("Empresas", "/empresas"),
    # a Equipe é uma aba da tela Empresas
    "equipe": LinkDeTela("Equipe", "/empresas?aba=equipe"),
}

_SEM_CONCILIACAO = "Esta empresa ainda não tem conciliação."
_MES_INVALIDO = "Não entendi o mês."
_MES_FUTURO = "Esse mês ainda não chegou."
_FORA_DA_JANELA = f"Eu consulto até {JANELA_MESES} meses atrás."
_SEM_CONCILIACAO_NA_CONTA = "Nenhuma empresa da conta tem conciliação."
_MESMO_MES = "Para comparar, preciso de dois meses diferentes."
EMPRESAS = TELAS_DE_ACAO["empresas"]


# --- formatação ----------------------------------------------------------------------


def _numero(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{_numero(n)} {singular if n == 1 else plural}"


def _reais(valor: Decimal) -> str:
    inteiro, centavos = f"{abs(valor):,.2f}".split(".")
    sinal = "-" if valor < 0 else ""
    return f"{sinal}R$ {inteiro.replace(',', '.')},{centavos}"


def _data(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def _mes(competencia: str) -> str:
    ano, mes = competencia.split("-")
    return f"{_MESES[int(mes) - 1]}/{ano}"


def _maiuscula(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def _juntar(partes: list[str]) -> str:
    if len(partes) <= 1:
        return "".join(partes)
    return f"{', '.join(partes[:-1])} e {partes[-1]}"


# --- mês -----------------------------------------------------------------------------

FUSO = ZoneInfo("America/Sao_Paulo")


def mes_atual_de_sao_paulo(instante: datetime) -> str:
    """O mês (AAAA-MM) de `instante` no fuso de São Paulo: "mês passado" é
    contado no fuso do cliente, não em UTC."""
    return instante.astimezone(FUSO).strftime("%Y-%m")


def _indice(ano: int, mes: int) -> int:
    return ano * 12 + mes - 1


def _competencia_do_indice(indice: int) -> str:
    return f"{indice // 12:04d}-{indice % 12 + 1:02d}"


def resolver_competencia(
    competencia: str | None, meses_atras: int | None, mes_atual: str
) -> str | None | Recusa:
    """A competência pedida (AAAA-MM), `None` quando a pergunta não citou mês
    (quem chama usa o mais recente com conciliação) ou `Recusa` quando o mês
    é inválido, futuro ou de mais de `JANELA_MESES` meses atrás."""
    atual = _PADRAO_COMPETENCIA.match(mes_atual)
    assert atual is not None, "mes_atual vem do backend, sempre AAAA-MM"
    indice_atual = _indice(int(atual[1]), int(atual[2]))
    if competencia is not None:
        pedido = _PADRAO_COMPETENCIA.match(competencia)
        if pedido is None:
            return Recusa(_MES_INVALIDO)
        indice = _indice(int(pedido[1]), int(pedido[2]))
    elif meses_atras is not None:
        if meses_atras < 0:
            return Recusa(_MES_INVALIDO)
        indice = indice_atual - meses_atras
    else:
        return None
    if indice > indice_atual:
        return Recusa(_MES_FUTURO)
    if indice_atual - indice > JANELA_MESES:
        return Recusa(_FORA_DA_JANELA)
    return _competencia_do_indice(indice)


def _mais_recente(db: Session, empresa_id: uuid.UUID) -> str | None:
    meses = api_publica.meses_com_conciliacao(db, empresa_id, limite=1).itens
    return meses[0].competencia if meses else None


# --- consultas -----------------------------------------------------------------------


def _links_das_conciliacoes(
    pares: list[fechamentos.ParDoMes], competencia: str
) -> tuple[LinkDeTela, ...]:
    mes = _mes(competencia)
    pares = pares[:MAXIMO_LINKS_DE_CONCILIACAO]
    if len(pares) == 1:
        titulos = [f"Conciliação de {mes}"]
    else:
        titulos = [f"Conciliação {i} de {mes}" for i in range(1, len(pares) + 1)]
    # com o `?sistema=`, como o `caminhoDaConciliacao` do front: sem ele, a
    # tela mistura as linhas de todos os pares do extrato do banco
    return tuple(
        LinkDeTela(titulo, f"/conciliacoes/{par.extrato_banco_id}?sistema={par.extrato_sistema_id}")
        for titulo, par in zip(titulos, pares, strict=True)
    )


def _sem_conciliacao_no_mes(competencia: str, recente: str | None) -> RespostaDeDados:
    if recente is None:
        return RespostaDeDados(_SEM_CONCILIACAO, (HISTORICO,))
    texto = f"{_maiuscula(_mes(competencia))} ainda não tem conciliação."
    if recente != competencia:
        texto += f" A mais recente é de {_mes(recente)}."
    return RespostaDeDados(texto, (HISTORICO,))


def _mais_antigo_da_janela(mes_atual: str) -> str:
    ano, mes = mes_atual.split("-")
    return _competencia_do_indice(_indice(int(ano), int(mes)) - JANELA_MESES)


def _meses(db: Session, empresa_id: uuid.UUID, mes_atual: str) -> RespostaDeDados:
    inicio = _mais_antigo_da_janela(mes_atual)
    itens = [
        item
        for item in api_publica.meses_com_conciliacao(db, empresa_id, limite=JANELA_MESES + 1).itens
        if inicio <= item.competencia <= mes_atual
    ]
    if not itens:
        return RespostaDeDados(_SEM_CONCILIACAO, (HISTORICO,))
    partes = [
        f"{_mes(item.competencia)} ({'fechado' if item.fechado else 'aberto'})"
        for item in itens[:MAXIMO_MESES_NO_TEXTO]
    ]
    if len(itens) > MAXIMO_MESES_NO_TEXTO:
        partes.append(_plural(len(itens) - MAXIMO_MESES_NO_TEXTO, "mês", "meses"))
    return RespostaDeDados(f"Meses com conciliação: {_juntar(partes)}.", (FECHAMENTOS, HISTORICO))


def _resumo(db: Session, empresa_id: uuid.UUID, competencia: str, pares) -> RespostaDeDados:
    resumo = api_publica.resumo_do_mes(db, empresa_id, competencia)
    c = resumo.contagens
    conciliadas = c.match_exato + c.match_tolerancia + c.match_agrupado
    divergencias = c.total - conciliadas
    texto = (
        f"{_maiuscula(_mes(competencia))}: {_plural(c.total, 'linha', 'linhas')}, "
        f"{_plural(conciliadas, 'conciliada', 'conciliadas')} e "
        f"{_plural(divergencias, 'divergência', 'divergências')}"
    )
    situacoes = [
        parte
        for n, parte in (
            (c.pendentes, f"{_numero(c.pendentes)} sem justificativa"),
            (c.aguardando_aprovacao, f"{_numero(c.aguardando_aprovacao)} aguardando aprovação"),
            (c.justificadas, _plural(c.justificadas, "justificada", "justificadas")),
        )
        if n
    ]
    if divergencias and situacoes:
        texto += f" ({_juntar(situacoes)})"
    texto += "."
    if c.linhas_nao_lidas:
        texto += f" {_plural(c.linhas_nao_lidas, 'linha do arquivo não foi lida', 'linhas do arquivo não foram lidas')}."
    if resumo.fechado:
        texto += " Mês fechado com ressalva." if resumo.com_ressalva else " Mês fechado."
    elif resumo.pronto_para_fechar:
        texto += " Mês aberto e pronto para fechar."
    else:
        texto += " Mês aberto."
    links = _links_das_conciliacoes(pares, competencia)[: MAXIMO_LINKS_DE_CONCILIACAO - 1]
    return RespostaDeDados(texto, (*links, FECHAMENTOS))


def _pendentes(linhas) -> int:
    return sum(1 for linha in linhas if linha.situacao in ("sem_justificativa", "conferida"))


def _sem_justificativa(n: int) -> str:
    if n == 0:
        return "Nenhuma está sem justificativa."
    return f"{_numero(n)} {'está' if n == 1 else 'estão'} sem justificativa."


def _divergencias(
    db: Session, empresa_id: uuid.UUID, competencia: str, pares, categoria: str | None
) -> RespostaDeDados:
    linhas = api_publica.linhas_divergentes(db, empresa_id, pares)
    if categoria is not None:
        linhas = [linha for linha in linhas if linha.categoria == categoria]
    mes = _maiuscula(_mes(competencia))
    links = _links_das_conciliacoes(pares, competencia)
    # Como o resumo do mês (`fechamentos.resumo_do_mes`) e a Visão geral: a
    # conferida não tem justificativa e continua pendente.
    pendentes = _pendentes(linhas)
    if categoria is not None:
        singular, plural = _CATEGORIAS[categoria]
        if not linhas:
            return RespostaDeDados(f"{mes} não tem {singular}.", links)
        texto = (
            f"{mes} tem {_plural(len(linhas), singular, plural)}. {_sem_justificativa(pendentes)}"
        )
        return RespostaDeDados(texto, links)
    if not linhas:
        return RespostaDeDados(f"{mes} não tem divergência.", links)
    por_categoria = Counter(linha.categoria for linha in linhas)
    partes = [
        _plural(por_categoria[nome], *_CATEGORIAS[nome])
        for nome in _CATEGORIAS
        if por_categoria[nome]
    ]
    texto = (
        f"{mes} tem {_plural(len(linhas), 'divergência', 'divergências')}: "
        f"{_juntar(partes)}. {_sem_justificativa(pendentes)}"
    )
    return RespostaDeDados(texto, links)


def _abertas(linhas) -> list:
    return [linha for linha in linhas if linha.situacao != "justificada"]


def _valor_em_aberto(abertas) -> Decimal:
    return Decimal(
        sum(
            (
                abs(linha.valor - linha.valor_sistema)
                if linha.valor_sistema is not None
                else abs(linha.valor)
            )
            for linha in abertas
        )
    )


def _em_aberto(db: Session, empresa_id: uuid.UUID, competencia: str, pares) -> RespostaDeDados:
    abertas = _abertas(api_publica.linhas_divergentes(db, empresa_id, pares))
    total = _valor_em_aberto(abertas)
    links = _links_das_conciliacoes(pares, competencia)
    mes = _mes(competencia)
    if not abertas:
        return RespostaDeDados(f"{_maiuscula(mes)} não tem valor em aberto.", links)
    linhas = _plural(len(abertas), "linha que ainda pede", "linhas que ainda pedem")
    if not total:
        return RespostaDeDados(
            f"{_maiuscula(mes)} não tem valor em aberto, mas {linhas} decisão.", links
        )
    return RespostaDeDados(
        f"Em {mes} ficaram {_reais(total)} em aberto, em {linhas} decisão.", links
    )


def _trecho_do_saldo(db: Session, extrato: Extrato) -> str | None:
    saldo = saldo_do_extrato(db, extrato)
    if saldo.saldo_final is None or saldo.saldo_data is None:
        return None
    final = f"saldo final de {_reais(saldo.saldo_final)} em {_data(saldo.saldo_data)}"
    if saldo.saldo_inicial is None:
        return (
            f"{final}; o saldo inicial não dá para calcular, porque há linhas do arquivo "
            "que não foram lidas"
        )
    return f"saldo inicial de {_reais(saldo.saldo_inicial)} e {final}"


def _saldo(db: Session, empresa_id: uuid.UUID, competencia: str, pares) -> RespostaDeDados:
    links = _links_das_conciliacoes(pares, competencia)
    mes = _mes(competencia)
    trechos = [
        _trecho_do_saldo(db, extrato)
        for extrato in (db.get(Extrato, par.extrato_banco_id) for par in pares)
        if extrato is not None and extrato.empresa_id == empresa_id
    ]
    if len(trechos) == 1:
        (trecho,) = trechos
        if trecho is None:
            return RespostaDeDados(
                f"O arquivo do extrato do banco de {mes} não informa o saldo.", links
            )
        return RespostaDeDados(f"Extrato do banco de {mes}: {trecho}.", links)
    partes = [
        f"{i}º: {trecho or 'o arquivo não informa o saldo'}."
        for i, trecho in enumerate(trechos, start=1)
    ]
    texto = f"{_maiuscula(mes)} tem {_numero(len(trechos))} extratos do banco. {' '.join(partes)}"
    return RespostaDeDados(texto, links)


# --- comparar meses ----------------------------------------------------------------


@dataclass(frozen=True)
class _NumerosDoMes:
    divergencias: int
    pendentes: int
    em_aberto: Decimal
    por_categoria: Counter


def _numeros_do_mes(db: Session, empresa_id: uuid.UUID, pares) -> _NumerosDoMes:
    linhas = api_publica.linhas_divergentes(db, empresa_id, pares)
    return _NumerosDoMes(
        divergencias=len(linhas),
        pendentes=_pendentes(linhas),
        em_aberto=_valor_em_aberto(_abertas(linhas)),
        por_categoria=Counter(linha.categoria for linha in linhas),
    )


def _diferenca(a: int, b: int) -> str:
    if a == b:
        return "igual"
    return f"{_numero(abs(a - b))} a {'mais' if a > b else 'menos'}"


def _comparacao_de_contagem(a: int, b: int, singular: str, plural: str) -> str:
    if a == b:
        return f"{_plural(a, singular, plural)} nos dois meses"
    return f"{_plural(a, singular, plural)} contra {_numero(b)} ({_diferenca(a, b)})"


def _comparacao_em_aberto(a: Decimal, b: Decimal) -> str:
    if a == b:
        if not a:
            return "nenhum valor em aberto nos dois meses"
        return f"{_reais(a)} em aberto nos dois meses"
    sentido = "a mais" if a > b else "a menos"
    return f"{_reais(a)} em aberto contra {_reais(b)} ({_reais(abs(a - b))} {sentido})"


def _comparar(
    db: Session,
    empresa_id: uuid.UUID,
    consulta: ConsultaDeDados,
    mes_atual: str,
    recente: str | None,
) -> RespostaDeDados:
    """Um mês contra outro. Sem o primeiro, o mais recente com conciliação;
    sem o segundo, o mês anterior ao primeiro. "De onde veio" é a diferença
    por categoria: os números explicam, o modelo não (ADR-028)."""
    principal = resolver_competencia(consulta.competencia, consulta.meses_atras, mes_atual)
    if isinstance(principal, Recusa):
        return RespostaDeDados(principal.texto, ())
    if principal is None:
        if recente is None:
            return RespostaDeDados(_SEM_CONCILIACAO, (HISTORICO,))
        principal = recente
    outro = resolver_competencia(
        consulta.comparar_com, consulta.comparar_com_meses_atras, mes_atual
    )
    if isinstance(outro, Recusa):
        return RespostaDeDados(outro.texto, ())
    if outro is None:
        ano, mes = principal.split("-")
        outro = _competencia_do_indice(_indice(int(ano), int(mes)) - 1)
        if isinstance(resolver_competencia(outro, None, mes_atual), Recusa):
            return RespostaDeDados(_FORA_DA_JANELA, ())
    if outro == principal:
        return RespostaDeDados(_MESMO_MES, ())
    pares_a = fechamentos.pares_do_mes(db, empresa_id, principal)
    pares_b = fechamentos.pares_do_mes(db, empresa_id, outro)
    for pares, sem, com in ((pares_a, principal, outro), (pares_b, outro, principal)):
        if not pares:
            texto = (
                f"{_maiuscula(_mes(sem))} não tem conciliação, então não dá para comparar "
                f"com {_mes(com)}."
            )
            return RespostaDeDados(texto, (HISTORICO,))
    a = _numeros_do_mes(db, empresa_id, pares_a)
    b = _numeros_do_mes(db, empresa_id, pares_b)
    partes = [
        _comparacao_de_contagem(a.divergencias, b.divergencias, "divergência", "divergências"),
        _comparacao_de_contagem(a.pendentes, b.pendentes, "sem justificativa", "sem justificativa"),
        _comparacao_em_aberto(a.em_aberto, b.em_aberto),
    ]
    texto = f"{_maiuscula(_mes(principal))} comparado com {_mes(outro)}: {_juntar(partes)}."
    origem = [
        f"{_CATEGORIAS[nome][1]} {a.por_categoria[nome] - b.por_categoria[nome]:+d}"
        for nome in _CATEGORIAS
        if a.por_categoria[nome] != b.por_categoria[nome]
    ]
    if origem:
        texto += f" Por tipo de divergência: {_juntar(origem)}."
    links = (
        *_links_das_conciliacoes(pares_a, principal)[:1],
        *_links_das_conciliacoes(pares_b, outro)[:1],
        HISTORICO,
    )
    return RespostaDeDados(texto, links)


# --- empresas da conta -------------------------------------------------------------


def _mais_recente_da_conta(db: Session, conta_id: uuid.UUID, mes_atual: str) -> str | None:
    mes = func.to_char(Extrato.periodo_inicio, "YYYY-MM")
    return db.scalar(
        select(func.max(mes))
        .join(ExecucaoConciliacao, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
        .join(Empresa, Empresa.id == ExecucaoConciliacao.empresa_id)
        .where(Empresa.conta_id == conta_id, mes <= mes_atual)
    )


def _nomes(nomes: list[str]) -> str:
    if len(nomes) > MAXIMO_EMPRESAS_POR_GRUPO:
        resto = len(nomes) - MAXIMO_EMPRESAS_POR_GRUPO
        nomes = [*nomes[:MAXIMO_EMPRESAS_POR_GRUPO], f"mais {_numero(resto)}"]
    return _juntar(nomes)


def _empresas_do_mes(
    db: Session, empresa_id: uuid.UUID, consulta: ConsultaDeDados, mes_atual: str
) -> RespostaDeDados:
    """A situação do mês em cada empresa da conta da empresa ativa: fechada,
    aberta (tem conciliação) ou sem conciliação. Quem acessa uma empresa da
    conta acessa todas (`_empresa_ativa`, app/core/auth.py). Três consultas."""
    ativa = db.get(Empresa, empresa_id)
    assert ativa is not None, "a rota já conferiu a empresa"
    conta_id = ativa.conta_id
    competencia = resolver_competencia(consulta.competencia, consulta.meses_atras, mes_atual)
    if isinstance(competencia, Recusa):
        return RespostaDeDados(competencia.texto, ())
    if competencia is None:
        competencia = _mais_recente_da_conta(db, conta_id, mes_atual)
        if competencia is None:
            return RespostaDeDados(_SEM_CONCILIACAO_NA_CONTA, (EMPRESAS,))
    empresas = list(
        db.execute(
            select(Empresa.id, Empresa.razao_social)
            .where(Empresa.conta_id == conta_id)
            .order_by(func.lower(Empresa.razao_social), Empresa.id)
        )
    )
    mes = func.to_char(Extrato.periodo_inicio, "YYYY-MM")
    com_conciliacao = set(
        db.scalars(
            select(ExecucaoConciliacao.empresa_id)
            .join(Extrato, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
            .join(Empresa, Empresa.id == ExecucaoConciliacao.empresa_id)
            .where(Empresa.conta_id == conta_id, mes == competencia)
        )
    )
    fechadas = set(
        db.scalars(
            select(Fechamento.empresa_id)
            .join(Empresa, Empresa.id == Fechamento.empresa_id)
            .where(
                Empresa.conta_id == conta_id,
                Fechamento.competencia == competencia,
                Fechamento.reaberto_em.is_(None),
            )
        )
    )
    grupos: dict[str, list[str]] = {"fechada": [], "aberta": [], "sem": []}
    for id_, nome in empresas:
        if id_ in fechadas:
            grupos["fechada"].append(nome)
        elif id_ in com_conciliacao:
            grupos["aberta"].append(nome)
        else:
            grupos["sem"].append(nome)
    partes = [
        f"{_plural(len(nomes), singular, plural)} ({_nomes(nomes)})"
        for chave, singular, plural in (
            ("fechada", "fechada", "fechadas"),
            ("aberta", "aberta", "abertas"),
            ("sem", "sem conciliação", "sem conciliação"),
        )
        if (nomes := grupos[chave])
    ]
    onde = (
        "na única empresa da conta"
        if len(empresas) == 1
        else f"nas {_numero(len(empresas))} empresas da conta"
    )
    texto = f"{_maiuscula(_mes(competencia))}, {onde}: {_juntar(partes)}."
    return RespostaDeDados(texto, (EMPRESAS,))


def nomes_das_empresas_da_conta(db: Session, conta_id: uuid.UUID) -> tuple[str, ...]:
    """As razões sociais da conta, para a rota trocá-las por `[EMPRESA]` na
    conversa antes de ela sair para o provedor."""
    return tuple(db.scalars(select(Empresa.razao_social).where(Empresa.conta_id == conta_id)))


def responder(
    db: Session, empresa_id: uuid.UUID, consulta: ConsultaDeDados, mes_atual: str
) -> RespostaDeDados:
    """A resposta da consulta, só com dados de `empresa_id`. `mes_atual` é o
    mês corrente de São Paulo (AAAA-MM)."""
    if consulta.intencao == "meses":
        return _meses(db, empresa_id, mes_atual)
    if consulta.intencao == "empresas_do_mes":
        return _empresas_do_mes(db, empresa_id, consulta, mes_atual)
    if consulta.intencao == "comparar_meses":
        return _comparar(db, empresa_id, consulta, mes_atual, _mais_recente(db, empresa_id))
    competencia = resolver_competencia(consulta.competencia, consulta.meses_atras, mes_atual)
    if isinstance(competencia, Recusa):
        return RespostaDeDados(competencia.texto, ())
    recente = _mais_recente(db, empresa_id)
    if competencia is None:
        if recente is None:
            return RespostaDeDados(_SEM_CONCILIACAO, (HISTORICO,))
        competencia = recente
    pares = fechamentos.pares_do_mes(db, empresa_id, competencia)
    if not pares:
        return _sem_conciliacao_no_mes(competencia, recente)
    if consulta.intencao == "resumo_do_mes":
        return _resumo(db, empresa_id, competencia, pares)
    if consulta.intencao == "divergencias_do_mes":
        return _divergencias(db, empresa_id, competencia, pares, consulta.categoria)
    if consulta.intencao == "em_aberto":
        return _em_aberto(db, empresa_id, competencia, pares)
    if consulta.intencao == "saldo":
        return _saldo(db, empresa_id, competencia, pares)
    raise ValueError(f"intenção desconhecida: {consulta.intencao}")


def executar_somente_leitura(db: Session, funcao: Callable[[], T]) -> T:
    """`funcao()` numa transação `READ ONLY` do Postgres, desfeita no fim. A
    transação que estiver aberta é desfeita antes."""
    db.rollback()
    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        return funcao()
    finally:
        db.rollback()
