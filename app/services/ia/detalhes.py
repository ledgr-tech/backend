"""Fatos formatados de uma divergência: linhas de detalhe, texto
determinístico, o bloco `fatos` do prompt e a validação do texto da IA (fix
da explicação detalhada, ADR-011).

Divisão de trabalho: todo número e toda data saem daqui, já calculados e
formatados em pt-BR; a IA só interpreta. O front renderiza `montar_detalhes`
como está, sem lógica de formatação dele.

Nenhuma saída deste módulo inclui a descrição do lançamento (nem a do
candidato): as linhas, o texto e os fatos usam só valor, data, contagens e
números derivados (a similaridade é um número de 0 a 100). Por isso o texto
pode ir para qualquer superfície.

Só afirma o que app/services/matching.py garante:
- `divergente_data` com par gravado: o motor associa as duas pontas quando o
  valor aparece uma única vez de cada lado entre as sobras, depois da
  passada de tolerância. Com a diferença acima da tolerância, a tolerância é
  a causa; com tolerância igual à diferença, o par entraria na passada de
  tolerância como candidato.
- `divergente_data` sem par gravado: os candidatos são lançamentos do outro
  lado com o mesmo valor em outra data, que podem ter ficado de fora da
  passada de tolerância (um `duplicado`, por exemplo). Diferença dentro da
  tolerância: frase neutra, sem afirmar a causa.
- `sem_correspondencia`: nenhum lançamento do outro lado que ficou sem par
  tem o mesmo valor ou a mesma data.
- `duplicado`: o grupo de mesmo valor e data formou pelo menos um par e este
  sobrou.
- `tarifa_bancaria`: a descrição tem um termo conhecido de tarifa. O termo
  não é exposto (o motor só devolve verdadeiro ou falso, e o termo é um
  pedaço da descrição).

Formatação escrita à mão, sem `locale`: `-R$ 1.234,56`, `dd/mm/aaaa`,
"1 dia"/"2 dias".
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.services.ia.base import CandidatoContexto, ContextoDivergencia, LancamentoContexto

_CENTAVOS = Decimal("0.01")


@dataclass(frozen=True)
class DetalheLinha:
    """Uma linha pronta para o front: rótulo e valor já formatados."""

    rotulo: str
    valor: str


# --- formatação pt-BR -----------------------------------------------------------------


def formatar_valor(valor: Decimal) -> str:
    """`-R$ 1.234,56`, `R$ 0,00`. Sinal antes do símbolo, como na tela."""
    arredondado = valor.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)
    sinal = "-" if arredondado < 0 else ""
    inteiro, centavos = f"{abs(arredondado):.2f}".split(".")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return f"{sinal}R$ {'.'.join(grupos)},{centavos}"


def formatar_data(data: date) -> str:
    return f"{data.day:02d}/{data.month:02d}/{data.year:04d}"


def formatar_dias(dias: int) -> str:
    return f"{dias} dia" if abs(dias) == 1 else f"{dias} dias"


def formatar_quantidade(quantidade: int) -> str:
    return f"{quantidade} lançamento" if quantidade == 1 else f"{quantidade} lançamentos"


def formatar_percentual(percentual: Decimal) -> str:
    return f"{percentual:.1f}".replace(".", ",") + "%"


def _lancamento(lancamento: LancamentoContexto) -> str:
    return f"{formatar_valor(lancamento.valor)} em {formatar_data(lancamento.data)}"


# --- leitura do contexto -------------------------------------------------------------


def _lados(contexto: ContextoDivergencia) -> tuple[str, str]:
    lado = contexto.lado_do_item or "banco"
    return lado, "sistema" if lado == "banco" else "banco"


def _principal(contexto: ContextoDivergencia) -> CandidatoContexto | None:
    return contexto.candidatos[0] if contexto.candidatos else None


def _pontas(
    contexto: ContextoDivergencia,
) -> tuple[LancamentoContexto | None, LancamentoContexto | None]:
    """(banco, sistema): o item e o candidato principal, cada um no seu lado."""
    lado, _ = _lados(contexto)
    candidato = _principal(contexto)
    outro = candidato.lancamento if candidato else None
    if lado == "banco":
        return contexto.lancamento, outro
    return outro, contexto.lancamento


def _sistema_menos_banco(contexto: ContextoDivergencia) -> int | None:
    banco, sistema = _pontas(contexto)
    if banco is None or sistema is None:
        return None
    return (sistema.data - banco.data).days


def _sentido(dias: int) -> str:
    if dias > 0:
        return f"o sistema lançou {formatar_dias(dias)} depois do banco"
    return f"o sistema lançou {formatar_dias(-dias)} antes do banco"


def _fora_do_periodo(contexto: ContextoDivergencia) -> bool | None:
    if contexto.periodo_outro_lado is None:
        return None
    inicio, fim = contexto.periodo_outro_lado
    return not inicio <= contexto.lancamento.data <= fim


def _periodo(contexto: ContextoDivergencia) -> str | None:
    if contexto.periodo_outro_lado is None:
        return None
    inicio, fim = contexto.periodo_outro_lado
    return f"{formatar_data(inicio)} a {formatar_data(fim)}"


def _diferenca_valor(candidato: CandidatoContexto) -> str:
    texto = formatar_valor(candidato.diferenca_valor)
    if candidato.diferenca_percentual is not None:
        texto += f" ({formatar_percentual(candidato.diferenca_percentual)})"
    return texto


# --- linhas de detalhe ---------------------------------------------------------------


def montar_detalhes(contexto: ContextoDivergencia) -> list[DetalheLinha]:
    """Linhas já formatadas para o front, por status. Nunca inclui
    descrição."""
    lado, outro = _lados(contexto)
    banco, sistema = _pontas(contexto)
    candidato = _principal(contexto)
    tolerancia = contexto.tolerancia_dias
    linhas: list[DetalheLinha] = []

    if banco is not None:
        linhas.append(DetalheLinha("Banco", _lancamento(banco)))
    if sistema is not None:
        linhas.append(DetalheLinha("Sistema", _lancamento(sistema)))

    status = contexto.status
    if status == "divergente_valor" and candidato is not None:
        linhas.append(DetalheLinha("Diferença de valor", _diferenca_valor(candidato)))
    if status == "divergente_data" and candidato is not None:
        dias = _sistema_menos_banco(contexto)
        linhas.append(
            DetalheLinha("Diferença de datas", f"{formatar_dias(abs(dias))} ({_sentido(dias)})")
        )

    if tolerancia is not None and status in (
        "divergente_data",
        "divergente_valor",
        "sem_correspondencia",
    ):
        linhas.append(DetalheLinha("Tolerância da conciliação", formatar_dias(tolerancia)))

    if status == "divergente_data" and candidato is not None:
        if tolerancia is not None and candidato.diferenca_dias > tolerancia:
            linhas.append(
                DetalheLinha(
                    "Tolerância mínima para o par", formatar_dias(candidato.diferenca_dias)
                )
            )
        if candidato.cruza_fim_de_semana:
            linhas.append(DetalheLinha("Cruza fim de semana", "Sim"))

    if candidato is not None and candidato.similaridade_descricao is not None:
        linhas.append(
            DetalheLinha("Similaridade das descrições", f"{candidato.similaridade_descricao}%")
        )
    if contexto.total_candidatos is not None and contexto.total_candidatos > 1:
        linhas.append(DetalheLinha(f"Candidatos no {outro}", f"{contexto.total_candidatos}"))

    if status == "sem_correspondencia" and contexto.periodo_outro_lado is not None:
        linhas.append(DetalheLinha(f"Período do extrato do {outro}", _periodo(contexto)))
        linhas.append(
            DetalheLinha("Data dentro do período", "Não" if _fora_do_periodo(contexto) else "Sim")
        )

    if status == "duplicado":
        if contexto.quantidade_mesmo_lado is not None:
            linhas.append(
                DetalheLinha(f"Lançamentos iguais no {lado}", str(contexto.quantidade_mesmo_lado))
            )
        if contexto.quantidade_outro_lado is not None:
            linhas.append(
                DetalheLinha(f"Lançamentos iguais no {outro}", str(contexto.quantidade_outro_lado))
            )

    return linhas


# --- texto determinístico ------------------------------------------------------------


def _frases_divergente_data(contexto: ContextoDivergencia) -> list[str]:
    lado, outro = _lados(contexto)
    candidato = _principal(contexto)
    item = contexto.lancamento
    if candidato is None:
        return [
            f"No {lado}: {_lancamento(item)}.",
            f"Não há lançamento disponível no {outro} com o mesmo valor em outra data.",
        ]
    banco, sistema = _pontas(contexto)
    dias = _sistema_menos_banco(contexto)
    frases = [
        f"No banco: {_lancamento(banco)}.",
        f"No sistema: {_lancamento(sistema)}.",
        f"O valor é o mesmo e {_sentido(dias)}.",
    ]
    tolerancia = contexto.tolerancia_dias
    diferenca = candidato.diferenca_dias
    if tolerancia is None:
        return frases
    if diferenca > tolerancia:
        if contexto.par_gravado:
            frases.append(
                f"A tolerância desta conciliação era de {formatar_dias(tolerancia)}, "
                "por isso o par ficou como divergência de data."
            )
            frases.append(
                f"Com tolerância de {formatar_dias(diferenca)}, ele passaria a ser "
                "candidato a match por tolerância."
            )
        else:
            frases.append(
                f"A tolerância desta conciliação era de {formatar_dias(tolerancia)}, "
                "menor que essa diferença."
            )
            frases.append(
                f"Com tolerância de {formatar_dias(diferenca)}, esse lançamento ficaria "
                "dentro da janela de data."
            )
    else:
        frases.append(
            f"A diferença está dentro da tolerância de {formatar_dias(tolerancia)}, mas o "
            "par não fechou; confira se há mais de um candidato."
        )
    return frases


def _frases_divergente_valor(contexto: ContextoDivergencia) -> list[str]:
    lado, outro = _lados(contexto)
    candidato = _principal(contexto)
    item = contexto.lancamento
    if candidato is None:
        return [
            f"No {lado}: {_lancamento(item)}.",
            f"Não há lançamento disponível no {outro} nessa data com outro valor.",
        ]
    banco, sistema = _pontas(contexto)
    frases = [
        f"No banco: {_lancamento(banco)}.",
        f"No sistema: {_lancamento(sistema)}.",
        f"A data é a mesma e a diferença de valor é de {_diferenca_valor(candidato)}.",
    ]
    if contexto.total_candidatos is not None and contexto.total_candidatos > 1:
        frases.append(
            f"Há {formatar_quantidade(contexto.total_candidatos)} no {outro} nessa data; "
            "este é o de valor mais próximo."
        )
    return frases


def _frases_sem_correspondencia(contexto: ContextoDivergencia) -> list[str]:
    lado, outro = _lados(contexto)
    frases = [
        f"No {lado}: {_lancamento(contexto.lancamento)}.",
        f"Nenhum lançamento do {outro} que ficou sem par tem o mesmo valor ou a mesma data.",
    ]
    periodo = _periodo(contexto)
    if periodo is not None:
        if _fora_do_periodo(contexto):
            frases.append(
                f"O extrato do {outro} cobre de {periodo}, e este lançamento está fora "
                "desse período."
            )
        else:
            frases.append(
                f"O extrato do {outro} cobre de {periodo}, e este lançamento está dentro "
                "desse período."
            )
    return frases


def _frases_duplicado(contexto: ContextoDivergencia) -> list[str]:
    lado, outro = _lados(contexto)
    frases = [f"No {lado}: {_lancamento(contexto.lancamento)}."]
    if contexto.quantidade_mesmo_lado is not None and contexto.quantidade_outro_lado is not None:
        frases.append(
            f"Com esse valor e essa data há {formatar_quantidade(contexto.quantidade_mesmo_lado)} "
            f"no {lado} e {formatar_quantidade(contexto.quantidade_outro_lado)} no {outro}."
        )
    frases.append("Os pares possíveis já foram formados e este lançamento sobrou.")
    return frases


def _frases_tarifa(contexto: ContextoDivergencia) -> list[str]:
    lado, _ = _lados(contexto)
    return [
        f"No {lado}: {_lancamento(contexto.lancamento)}.",
        (
            "A descrição corresponde a um termo conhecido de tarifa bancária, por isso o "
            "lançamento foi separado como tarifa."
        ),
        "Confira se a tarifa foi registrada no sistema.",
    ]


_FRASES = {
    "divergente_data": _frases_divergente_data,
    "divergente_valor": _frases_divergente_valor,
    "sem_correspondencia": _frases_sem_correspondencia,
    "duplicado": _frases_duplicado,
    "tarifa_bancaria": _frases_tarifa,
}


def texto_deterministico(contexto: ContextoDivergencia) -> str:
    """Explicação em português com os números, sem IA e sem descrição.
    `ValueError` para status sem explicação (o mesmo de
    `motivo_deterministico`)."""
    if contexto.status not in _FRASES:
        raise ValueError(
            f"Status '{contexto.status}' não é elegível para explicação de divergência."
        )
    return " ".join(_FRASES[contexto.status](contexto))


# --- fatos para o prompt -------------------------------------------------------------


def _fatos_candidato(candidato: CandidatoContexto) -> dict:
    fatos = {
        "valor": formatar_valor(candidato.lancamento.valor),
        "data": formatar_data(candidato.lancamento.data),
    }
    if candidato.diferenca_dias is not None:
        fatos["dias_de_diferenca"] = formatar_dias(candidato.diferenca_dias)
    if candidato.diferenca_valor is not None:
        fatos["diferenca_valor"] = formatar_valor(candidato.diferenca_valor)
    if candidato.diferenca_percentual is not None:
        fatos["diferenca_percentual"] = formatar_percentual(candidato.diferenca_percentual)
    if candidato.similaridade_descricao is not None:
        fatos["similaridade_descricao"] = f"{candidato.similaridade_descricao}%"
    if candidato.cruza_fim_de_semana is not None:
        fatos["cruza_fim_de_semana"] = "sim" if candidato.cruza_fim_de_semana else "não"
    return fatos


def fatos_formatados(contexto: ContextoDivergencia) -> dict:
    """Os fatos já formatados, como a IA deve copiá-los. Chaves ausentes
    quando o dado não existe. Determinístico (entra na serialização e,
    por ela, na chave do cache)."""
    lado, outro = _lados(contexto)
    candidato = _principal(contexto)
    fatos: dict = {
        f"valor_{lado}": formatar_valor(contexto.lancamento.valor),
        f"data_{lado}": formatar_data(contexto.lancamento.data),
    }
    if candidato is not None:
        fatos[f"valor_{outro}"] = formatar_valor(candidato.lancamento.valor)
        fatos[f"data_{outro}"] = formatar_data(candidato.lancamento.data)
        fatos["candidatos"] = [_fatos_candidato(c) for c in contexto.candidatos]
    if contexto.status == "divergente_data" and candidato is not None:
        dias = _sistema_menos_banco(contexto)
        fatos["dias_de_diferenca"] = formatar_dias(abs(dias))
        fatos["sentido"] = _sentido(dias)
        if contexto.tolerancia_dias is not None and abs(dias) > contexto.tolerancia_dias:
            fatos["tolerancia_minima_para_o_par"] = formatar_dias(abs(dias))
    if contexto.status == "divergente_valor" and candidato is not None:
        fatos["diferenca_valor"] = formatar_valor(candidato.diferenca_valor)
        if candidato.diferenca_percentual is not None:
            fatos["diferenca_percentual"] = formatar_percentual(candidato.diferenca_percentual)
    if contexto.tolerancia_dias is not None:
        fatos["tolerancia_dias"] = formatar_dias(contexto.tolerancia_dias)
    if contexto.total_candidatos is not None:
        fatos["total_candidatos"] = str(contexto.total_candidatos)
    if contexto.periodo_outro_lado is not None:
        inicio, fim = contexto.periodo_outro_lado
        fatos["periodo_outro_lado_inicio"] = formatar_data(inicio)
        fatos["periodo_outro_lado_fim"] = formatar_data(fim)
        fatos["fora_do_periodo_outro_lado"] = "sim" if _fora_do_periodo(contexto) else "não"
    if contexto.quantidade_mesmo_lado is not None:
        fatos["quantidade_mesmo_lado"] = formatar_quantidade(contexto.quantidade_mesmo_lado)
    if contexto.quantidade_outro_lado is not None:
        fatos["quantidade_outro_lado"] = formatar_quantidade(contexto.quantidade_outro_lado)
    return fatos


# --- validação do texto da IA --------------------------------------------------------

_RE_DATA = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_RE_VALOR = re.compile(r"R\$\s*-?\s*(\d[\d.]*(?:,\d+)?)")
_RE_DIAS = re.compile(r"\b(\d+)\s+dias?\b", re.IGNORECASE)


def _valor_normalizado(bruto: str) -> Decimal | None:
    try:
        return Decimal(bruto.rstrip(".").replace(".", "").replace(",", "."))
    except ArithmeticError:
        return None


def _numeros(texto: str) -> tuple[set[str], set[Decimal | None], set[int]]:
    datas = set(_RE_DATA.findall(texto))
    valores = {_valor_normalizado(v) for v in _RE_VALOR.findall(texto)}
    dias = {int(n) for n in _RE_DIAS.findall(texto)}
    return datas, valores, dias


# Fim de frase: pontuação seguida de espaço. "R$ 1.234,56" e "01/10/2026" não
# têm espaço depois do ponto ou da barra, então nunca quebram.
_RE_FIM_DE_FRASE = re.compile(r"(?<=[.!?])\s+")


def dividir_frases(texto: str) -> list[str]:
    return [frase for frase in _RE_FIM_DE_FRASE.split(texto.strip()) if frase]


def _permitidos(contexto: ContextoDivergencia):
    return _numeros(json.dumps(fatos_formatados(contexto), ensure_ascii=False))


def _dentro_dos_fatos(trecho: str, permitidos) -> bool:
    permitidas, valores_permitidos, dias_permitidos = permitidos
    datas, valores, dias = _numeros(trecho)
    return datas <= permitidas and valores <= valores_permitidos and dias <= dias_permitidos


def texto_respeita_fatos(texto: str, contexto: ContextoDivergencia) -> bool:
    """Toda data (`dd/mm/aaaa`), todo valor (`R$`, sem olhar o sinal) e todo
    número seguido de "dia"/"dias" do texto precisa existir nos fatos. Um
    número que não está lá foi inventado ou calculado pelo modelo."""
    return _dentro_dos_fatos(texto, _permitidos(contexto))


def filtrar_texto_pelos_fatos(texto: str, contexto: ContextoDivergencia) -> str | None:
    """O texto só com as frases cujos números estão todos nos fatos (a mesma
    regra de `texto_respeita_fatos`, aplicada por frase), juntas com um
    espaço. `None` quando nenhuma frase sobra."""
    permitidos = _permitidos(contexto)
    frases = [f for f in dividir_frases(texto) if _dentro_dos_fatos(f, permitidos)]
    return " ".join(frases) if frases else None
