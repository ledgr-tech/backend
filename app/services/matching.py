"""Motor de matching (issues #16, #22 e #23): concilia os lançamentos de um
extrato do banco com os de um extrato do sistema, pela regra da ADR-006.

Dividido em camadas, pra que a regra seja testável sem banco:
- `parear_lancamentos`: núcleo puro do motor exato (ADR-006), com o
  pareamento dentro de cada grupo decidido por similaridade de descrição
  quando há ambiguidade (issue #23, ADR-008). Recebe duas listas de
  lançamentos em memória e devolve a lista de resultados, sem tocar em banco.
- `parear_por_tolerancia`: núcleo puro da segunda passada (issue #22, ADR-008),
  sobre o que o motor exato deixou `sem_correspondencia`.
- `conciliar_extratos`: carrega os lançamentos (sempre filtrando por
  `empresa_id` e `extrato_id`, ADR-004), chama o núcleo e persiste em
  `conciliacoes` (schema da ADR-007).

Regra (ADR-006): os lançamentos são agrupados pela chave (valor com sinal,
data), em dict, nos dois extratos. Com n do banco e m do sistema, formam-se
min(n, m) pares (`match_exato`). Os excedentes de qualquer lado ficam
`duplicado` se o grupo formou pelo menos um par, e `sem_correspondencia` se
não formou nenhum. Chave presente só num dos lados também é
`sem_correspondencia`. Dentro de um grupo sem ambiguidade (um candidato de
cada lado, ou um lado vazio) o par é o único possível. Com mais de um
candidato em pelo menos um dos lados, quem casa com quem é decidido por
similaridade de descrição (issue #23, ADR-008, ver docstring de
`parear_lancamentos`) — valor e data exatos já bastam pra confirmar o match;
a similaridade só desempata a ambiguidade de quem casa com quem, não entra em
`score_confianca`.

Bucket e custo (issue #19): a chave (valor, data) é o bucket do motor exato.
Cada lançamento cai num dict por chave, então nunca há comparação de todos
contra todos: o custo é linear no número de lançamentos, mais a ordenação
dentro de cada grupo (n log n só no pior caso, um grupo gigante). Os testes de
volume (tests/test_matching_volume.py e tests/test_conciliacoes_volume.py)
guardam esse comportamento.

Tolerância de data (issue #22, ADR-008): segunda passada, só quando
`Configuracao.tolerancia_dias_default` da empresa é maior que 0 (default 0
dias corridos, sem linha de Configuracao também vale 0; sem calendário de dia
útil/feriado). Recebe apenas os `sem_correspondencia` da passada exata; os
`duplicado` são decisão exclusiva da ADR-006 e nunca entram. Gera os pares
candidatos (mesmo valor, diferença de datas <= tolerância), ordena por chave
determinística (diferença de dias, datas, descrições normalizadas,
ocorrências) e casa greedy 1:1: `match_tolerancia`, regra `tolerancia`, score
1 - dias/(tolerância+1). A geração de candidatos é por valor (bucket por
valor), então não compara todos contra todos, só os de mesmo valor. O
desempate de candidatos com a mesma diferença de dias usa similaridade de
descrição (issue #23) em vez de comparar a descrição como string simples.

Similaridade de descrição (issue #23, ADR-008): `_similaridade_descricao` é
max(`rapidfuzz.fuzz.token_sort_ratio`, `fuzz.token_set_ratio`) entre duas
descrições — tolera palavras reordenadas (`token_sort_ratio`) e descrições
truncadas/com palavras a mais de um lado (`token_set_ratio`). Usada em dois
lugares: (1) dentro de `parear_lancamentos`, pra decidir quem casa com quem
quando um grupo de mesmo valor+data tem mais de um candidato em algum dos
lados — o pareamento vira guloso por similaridade decrescente, com o
desempate de hoje (descrição normalizada, ocorrência) só entrando quando a
similaridade empata; (2) como critério de desempate em `parear_por_tolerancia`
entre candidatos com a mesma diferença de dias. Em nenhum dos dois casos a
similaridade entra em `score_confianca` ou muda `status`/`regra_aplicada` —
valor+data exatos (ou dentro da tolerância) já são a confirmação do match, a
similaridade só resolve qual combinação é a certa. Em `parear_lancamentos`,
o grupo (mesmo valor+data) que tiver mais de `LIMITE_CANDIDATOS_PARA_
SIMILARIDADE` candidatos num dos lados volta pro pareamento posicional só
pra ele — cap de escala, não de produto, e por grupo, não pra conciliação
inteira; os outros grupos da mesma chamada continuam usando similaridade
normalmente (custo O(n·m) da comparação de todas as combinações não escala
pra grupo gigante — ver docstring da constante).

Não faz log de descrição, valor nem qualquer dado de lançamento (auditoria
de log fica pra Sprint 7).
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz
from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session

from app.models import Conciliacao, Configuracao, Lancamento
from app.services.normalizacao import _normalizar_descricao

STATUS_MATCH_EXATO = "match_exato"
STATUS_DUPLICADO = "duplicado"
STATUS_SEM_CORRESPONDENCIA = "sem_correspondencia"
STATUS_MATCH_TOLERANCIA = "match_tolerancia"
REGRA_EXATO = "exato"
REGRA_TOLERANCIA = "tolerancia"
SCORE_EXATO = Decimal("1.000")

# Cap de ESCALA, não de produto — não confundir com os thresholds de
# similaridade/tolerância da ADR-008 (esses decidem SE/COMO um par casa; este
# aqui só decide se vale a pena comparar todas as combinações de um grupo).
# Acima desse número de candidatos num dos lados de UM grupo (mesmo
# valor+data), comparar todas as combinações banco x sistema desse grupo
# vira O(n·m) — com milhares de candidatos dos dois lados isso não termina em
# tempo hábil (ver tests/test_matching_volume.py::
# test_grupo_gigante_com_a_mesma_chave_gera_20k_matches_dentro_do_teto, 20k de
# cada lado, motivo original da issue #19: custo linear no total de
# lançamentos). Grupos assim são o caso patológico desse teste de volume, não
# um cenário de ambiguidade genuína de poucas transações. O fallback é só
# para O GRUPO que estourou o cap — cada grupo é decidido independentemente
# em `parear_lancamentos` (ver `_parear_grupo`), então um único grupo gigante
# num extrato não desliga a similaridade pros outros grupos da mesma
# conciliação.
LIMITE_CANDIDATOS_PARA_SIMILARIDADE = 200


@dataclass(frozen=True)
class LancamentoParaMatching:
    """Recorte mínimo de um lançamento que o motor precisa."""

    id: uuid.UUID
    valor: Decimal
    data: date
    descricao: str
    ocorrencia: int


@dataclass(frozen=True)
class ResultadoMatching:
    """Uma linha futura de `conciliacoes`. Nos excedentes e sem par, só o
    lado correspondente tem id, e regra/score ficam nulos."""

    status: str
    lancamento_banco_id: uuid.UUID | None
    lancamento_sistema_id: uuid.UUID | None
    regra_aplicada: str | None = None
    score_confianca: Decimal | None = None


def _agrupar(
    lancamentos: list[LancamentoParaMatching],
) -> dict[tuple[Decimal, date], list[LancamentoParaMatching]]:
    grupos: dict[tuple[Decimal, date], list[LancamentoParaMatching]] = defaultdict(list)
    for lancamento in lancamentos:
        grupos[(lancamento.valor, lancamento.data)].append(lancamento)
    return grupos


def _ordenar(grupo: list[LancamentoParaMatching]) -> list[LancamentoParaMatching]:
    # sorted é estável: empates completos mantêm a ordem de entrada.
    return sorted(grupo, key=lambda item: (_normalizar_descricao(item.descricao), item.ocorrencia))


def _similaridade_descricao(descricao_a: str, descricao_b: str) -> float:
    """max(token_sort_ratio, token_set_ratio) do rapidfuzz (issue #23,
    ADR-008): token_sort_ratio tolera palavras em ordem diferente
    ("TED MARIA OLIVEIRA" x "OLIVEIRA MARIA TED"), token_set_ratio tolera
    palavras a mais/truncamento de um dos lados ("PIX RECEBIDO JOAO DA
    SILVA" x "JOAO SILVA")."""
    return max(
        fuzz.token_sort_ratio(descricao_a, descricao_b),
        fuzz.token_set_ratio(descricao_a, descricao_b),
    )


def _parear_grupo(
    lado_banco: list[LancamentoParaMatching], lado_sistema: list[LancamentoParaMatching]
) -> list[tuple[LancamentoParaMatching, LancamentoParaMatching]]:
    """Pares dentro de um grupo de mesmo valor+data (ADR-006), em ordem
    determinística. Sem ambiguidade (um candidato de cada lado, ou um lado
    vazio) o par é o único possível. Com mais de um candidato em algum dos
    lados (issue #23), guloso por similaridade de descrição decrescente,
    desempatado por (descrição normalizada, ocorrência) de cada lado — o
    mesmo desempate de antes, que só decide quando a similaridade empata
    (ex: descrições idênticas, sempre 100). Acima de
    `LIMITE_CANDIDATOS_PARA_SIMILARIDADE` candidatos num dos lados, volta pro
    pareamento posicional (ver docstring da constante)."""
    sem_ambiguidade = len(lado_banco) <= 1 and len(lado_sistema) <= 1
    grupo_grande_demais = (
        len(lado_banco) > LIMITE_CANDIDATOS_PARA_SIMILARIDADE
        or len(lado_sistema) > LIMITE_CANDIDATOS_PARA_SIMILARIDADE
    )
    if sem_ambiguidade or grupo_grande_demais:
        return list(zip(lado_banco, lado_sistema, strict=False))

    # índice de cada lado entra como último desempate, só pra garantir uma
    # ordem total mesmo no caso (não esperado em dado real) de dois itens
    # idênticos em descrição normalizada e ocorrência do mesmo lado — sem
    # isso, a comparação cairia nos próprios objetos, que não têm ordem.
    combinacoes = sorted(
        (
            (
                -_similaridade_descricao(do_banco.descricao, do_sistema.descricao),
                _normalizar_descricao(do_banco.descricao),
                do_banco.ocorrencia,
                _normalizar_descricao(do_sistema.descricao),
                do_sistema.ocorrencia,
                indice_banco,
                indice_sistema,
            ),
            do_banco,
            do_sistema,
        )
        for indice_banco, do_banco in enumerate(lado_banco)
        for indice_sistema, do_sistema in enumerate(lado_sistema)
    )

    pares: list[tuple[LancamentoParaMatching, LancamentoParaMatching]] = []
    usados_banco: set[uuid.UUID] = set()
    usados_sistema: set[uuid.UUID] = set()
    for _, do_banco, do_sistema in combinacoes:
        if do_banco.id in usados_banco or do_sistema.id in usados_sistema:
            continue
        usados_banco.add(do_banco.id)
        usados_sistema.add(do_sistema.id)
        pares.append((do_banco, do_sistema))
    return pares


def parear_lancamentos(
    banco: list[LancamentoParaMatching], sistema: list[LancamentoParaMatching]
) -> list[ResultadoMatching]:
    """Núcleo puro do motor exato (ADR-006, com o desempate de pareamento por
    similaridade da issue #23). Determinístico: a mesma entrada gera sempre o
    mesmo pareamento, e os grupos saem em ordem de (data, valor)."""
    grupos_banco = _agrupar(banco)
    grupos_sistema = _agrupar(sistema)
    chaves = sorted(set(grupos_banco) | set(grupos_sistema), key=lambda c: (c[1], c[0]))

    resultados: list[ResultadoMatching] = []
    for chave in chaves:
        lado_banco = _ordenar(grupos_banco.get(chave, []))
        lado_sistema = _ordenar(grupos_sistema.get(chave, []))
        pares = _parear_grupo(lado_banco, lado_sistema)
        usados_banco = {do_banco.id for do_banco, _ in pares}
        usados_sistema = {do_sistema.id for _, do_sistema in pares}

        for do_banco, do_sistema in pares:
            resultados.append(
                ResultadoMatching(
                    status=STATUS_MATCH_EXATO,
                    lancamento_banco_id=do_banco.id,
                    lancamento_sistema_id=do_sistema.id,
                    regra_aplicada=REGRA_EXATO,
                    score_confianca=SCORE_EXATO,
                )
            )

        status_excedente = STATUS_DUPLICADO if pares else STATUS_SEM_CORRESPONDENCIA
        for excedente in lado_banco:
            if excedente.id not in usados_banco:
                resultados.append(ResultadoMatching(status_excedente, excedente.id, None))
        for excedente in lado_sistema:
            if excedente.id not in usados_sistema:
                resultados.append(ResultadoMatching(status_excedente, None, excedente.id))
    return resultados


def parear_por_tolerancia(
    banco_sem_par: list[LancamentoParaMatching],
    sistema_sem_par: list[LancamentoParaMatching],
    tolerancia_dias: int,
) -> list[ResultadoMatching]:
    """Segunda passada (issue #22, ADR-008) sobre os `sem_correspondencia` da
    passada exata. Pura e determinística; ver docstring do módulo."""

    def _sem_correspondencia() -> list[ResultadoMatching]:
        return [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, item.id, None) for item in banco_sem_par
        ] + [
            ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, None, item.id) for item in sistema_sem_par
        ]

    if tolerancia_dias <= 0 or not banco_sem_par or not sistema_sem_par:
        return _sem_correspondencia()

    sistema_por_valor: dict[Decimal, list[LancamentoParaMatching]] = defaultdict(list)
    for do_sistema in sistema_sem_par:
        sistema_por_valor[do_sistema.valor].append(do_sistema)

    candidatos = []
    for do_banco in banco_sem_par:
        for do_sistema in sistema_por_valor.get(do_banco.valor, []):
            dias = abs((do_banco.data - do_sistema.data).days)
            if dias <= tolerancia_dias:
                candidatos.append(
                    (
                        (
                            dias,
                            -_similaridade_descricao(do_banco.descricao, do_sistema.descricao),
                            do_banco.data,
                            do_sistema.data,
                            _normalizar_descricao(do_banco.descricao),
                            do_banco.ocorrencia,
                            _normalizar_descricao(do_sistema.descricao),
                            do_sistema.ocorrencia,
                        ),
                        do_banco,
                        do_sistema,
                    )
                )
    candidatos.sort(key=lambda candidato: candidato[0])

    resultados: list[ResultadoMatching] = []
    usados_banco: set[uuid.UUID] = set()
    usados_sistema: set[uuid.UUID] = set()
    for chave, do_banco, do_sistema in candidatos:
        if do_banco.id in usados_banco or do_sistema.id in usados_sistema:
            continue
        usados_banco.add(do_banco.id)
        usados_sistema.add(do_sistema.id)
        resultados.append(
            ResultadoMatching(
                status=STATUS_MATCH_TOLERANCIA,
                lancamento_banco_id=do_banco.id,
                lancamento_sistema_id=do_sistema.id,
                regra_aplicada=REGRA_TOLERANCIA,
                score_confianca=round(
                    Decimal(1) - Decimal(chave[0]) / Decimal(tolerancia_dias + 1), 3
                ),
            )
        )

    for item in banco_sem_par:
        if item.id not in usados_banco:
            resultados.append(ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, item.id, None))
    for item in sistema_sem_par:
        if item.id not in usados_sistema:
            resultados.append(ResultadoMatching(STATUS_SEM_CORRESPONDENCIA, None, item.id))
    return resultados


def _chave_lock_do_par(
    empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID, extrato_sistema_id: uuid.UUID
) -> int:
    """Inteiro de 64 bits com sinal, derivado de forma determinística do par
    (primeiros 8 bytes de um sha256 dos três ids), pra `pg_advisory_xact_lock`."""
    bruto = f"{empresa_id}|{extrato_banco_id}|{extrato_sistema_id}".encode()
    return int.from_bytes(hashlib.sha256(bruto).digest()[:8], "big", signed=True)


def _carregar_lancamentos(
    db: Session, empresa_id: uuid.UUID, extrato_id: uuid.UUID
) -> list[LancamentoParaMatching]:
    linhas = db.execute(
        select(
            Lancamento.id,
            Lancamento.valor,
            Lancamento.data,
            Lancamento.descricao,
            Lancamento.ocorrencia,
        ).where(Lancamento.empresa_id == empresa_id, Lancamento.extrato_id == extrato_id)
    ).all()
    return [
        LancamentoParaMatching(
            id=linha.id,
            valor=linha.valor,
            data=linha.data,
            descricao=linha.descricao,
            ocorrencia=linha.ocorrencia,
        )
        for linha in linhas
    ]


def conciliar_extratos(
    db: Session,
    empresa_id: uuid.UUID,
    extrato_banco_id: uuid.UUID,
    extrato_sistema_id: uuid.UUID,
) -> dict[str, int]:
    """Roda o motor exato pro par de extratos e persiste em `conciliacoes`.

    Idempotente (ADR-007): apaga as conciliações anteriores do par
    (empresa_id, extrato_banco_id, extrato_sistema_id) e grava as novas na
    mesma transação, em insert em lote. Devolve as contagens por status.
    Quem chama valida empresa, origem e status dos extratos antes.

    Concorrência: a transação começa adquirindo `pg_advisory_xact_lock` com
    uma chave derivada do par, então duas execuções simultâneas do mesmo par
    se enfileiram (a segunda espera a primeira dar commit e depois apaga e
    regrava) em vez de duplicar linhas. O lock solta sozinho no commit ou
    rollback; pares diferentes não se bloqueiam.
    """
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:chave)"),
            {"chave": _chave_lock_do_par(empresa_id, extrato_banco_id, extrato_sistema_id)},
        )
        banco = _carregar_lancamentos(db, empresa_id, extrato_banco_id)
        sistema = _carregar_lancamentos(db, empresa_id, extrato_sistema_id)
        resultados = parear_lancamentos(banco, sistema)

        tolerancia_dias = db.execute(
            select(Configuracao.tolerancia_dias_default).where(
                Configuracao.empresa_id == empresa_id
            )
        ).scalar_one_or_none()
        if tolerancia_dias:
            banco_por_id = {item.id: item for item in banco}
            sistema_por_id = {item.id: item for item in sistema}
            sem_par = [r for r in resultados if r.status == STATUS_SEM_CORRESPONDENCIA]
            resultados = [r for r in resultados if r.status != STATUS_SEM_CORRESPONDENCIA]
            resultados += parear_por_tolerancia(
                [banco_por_id[r.lancamento_banco_id] for r in sem_par if r.lancamento_banco_id],
                [
                    sistema_por_id[r.lancamento_sistema_id]
                    for r in sem_par
                    if r.lancamento_sistema_id
                ],
                tolerancia_dias,
            )

        db.execute(
            delete(Conciliacao).where(
                Conciliacao.empresa_id == empresa_id,
                Conciliacao.extrato_banco_id == extrato_banco_id,
                Conciliacao.extrato_sistema_id == extrato_sistema_id,
            )
        )
        if resultados:
            # Core na Table, não `insert(Conciliacao)` do ORM: o bulk insert do ORM
            # quebra o lote a cada mudança no padrão de campos nulos (par casado,
            # só banco, só sistema), gerando muitos statements, e cada um custa
            # uma ida e volta ao banco. Pelo Core tudo vai num único executemany.
            db.execute(
                insert(Conciliacao.__table__),
                [
                    {
                        "id": uuid.uuid4(),
                        "empresa_id": empresa_id,
                        "extrato_banco_id": extrato_banco_id,
                        "extrato_sistema_id": extrato_sistema_id,
                        "lancamento_banco_id": resultado.lancamento_banco_id,
                        "lancamento_sistema_id": resultado.lancamento_sistema_id,
                        "status": resultado.status,
                        "regra_aplicada": resultado.regra_aplicada,
                        "score_confianca": resultado.score_confianca,
                    }
                    for resultado in resultados
                ],
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "total": len(resultados),
        STATUS_MATCH_EXATO: sum(r.status == STATUS_MATCH_EXATO for r in resultados),
        STATUS_DUPLICADO: sum(r.status == STATUS_DUPLICADO for r in resultados),
        STATUS_SEM_CORRESPONDENCIA: sum(r.status == STATUS_SEM_CORRESPONDENCIA for r in resultados),
    }
