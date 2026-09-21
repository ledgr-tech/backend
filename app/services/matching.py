"""Motor de matching (issues #16 e #22): concilia os lançamentos de um extrato
do banco com os de um extrato do sistema, pela regra da ADR-006.

Dividido em camadas, pra que a regra seja testável sem banco:
- `parear_lancamentos`: núcleo puro do motor exato (ADR-006). Recebe duas
  listas de lançamentos em memória e devolve a lista de resultados, sem tocar
  em banco.
- `parear_por_tolerancia`: núcleo puro da segunda passada (issue #22, ADR-008),
  sobre o que o motor exato deixou `sem_correspondencia`.
- `conciliar_extratos`: carrega os lançamentos (sempre filtrando por
  `empresa_id` e `extrato_id`, ADR-004), chama o núcleo e persiste em
  `conciliacoes` (schema da ADR-007).

Regra (ADR-006): os lançamentos são agrupados pela chave (valor com sinal,
data), em dict, nos dois extratos. Dentro de cada grupo, cada lado é ordenado
por (descrição normalizada, ocorrencia) e pareado posição a posição, 1:1. Com
n do banco e m do sistema, formam-se min(n, m) pares (`match_exato`). Os
excedentes de qualquer lado ficam `duplicado` se o grupo formou pelo menos um
par, e `sem_correspondencia` se não formou nenhum. Chave presente só num dos
lados também é `sem_correspondencia`. A descrição só entra na ordenação
estável, nunca como critério de similaridade (isso é da Sprint 4).

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
valor), então não compara todos contra todos, só os de mesmo valor.

Não faz log de descrição, valor nem qualquer dado de lançamento (auditoria
de log fica pra Sprint 7).
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

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


def parear_lancamentos(
    banco: list[LancamentoParaMatching], sistema: list[LancamentoParaMatching]
) -> list[ResultadoMatching]:
    """Núcleo puro do motor exato (ADR-006). Determinístico: a mesma entrada
    gera sempre o mesmo pareamento, e os grupos saem em ordem de (data, valor)."""
    grupos_banco = _agrupar(banco)
    grupos_sistema = _agrupar(sistema)
    chaves = sorted(set(grupos_banco) | set(grupos_sistema), key=lambda c: (c[1], c[0]))

    resultados: list[ResultadoMatching] = []
    for chave in chaves:
        lado_banco = _ordenar(grupos_banco.get(chave, []))
        lado_sistema = _ordenar(grupos_sistema.get(chave, []))
        pares = min(len(lado_banco), len(lado_sistema))

        for do_banco, do_sistema in zip(lado_banco, lado_sistema, strict=False):
            resultados.append(
                ResultadoMatching(
                    status=STATUS_MATCH_EXATO,
                    lancamento_banco_id=do_banco.id,
                    lancamento_sistema_id=do_sistema.id,
                    regra_aplicada=REGRA_EXATO,
                    score_confianca=SCORE_EXATO,
                )
            )

        status_excedente = STATUS_DUPLICADO if pares > 0 else STATUS_SEM_CORRESPONDENCIA
        for excedente in lado_banco[pares:]:
            resultados.append(
                ResultadoMatching(status_excedente, excedente.id, None),
            )
        for excedente in lado_sistema[pares:]:
            resultados.append(
                ResultadoMatching(status_excedente, None, excedente.id),
            )
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
