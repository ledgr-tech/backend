"""Percentual de acerto de uma execução de conciliação (issue #27, ADR-010).

Calculado na leitura, nunca armazenado: `execucoes_conciliacao`
(app/models/execucao_conciliacao.py) só guarda as contagens brutas, e
qualquer mudança futura na fórmula não exige backfill.
"""

from decimal import ROUND_HALF_UP, Decimal


def calcular_percentual_acerto(
    match_exato: int, match_tolerancia: int, total: int
) -> Decimal | None:
    """(match_exato + match_tolerancia) / total, em porcentagem com duas
    casas (arredondamento meio para cima). `None` quando `total` é zero —
    não existe percentual de acerto de uma execução sem nenhum lançamento."""
    if total == 0:
        return None
    acertos = Decimal(match_exato + match_tolerancia)
    percentual = acertos / Decimal(total) * Decimal(100)
    return percentual.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
