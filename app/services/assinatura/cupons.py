"""Os cupons da Stripe que o operador pode dar a uma conta (issue #169,
ADR-025 e ADR-026), lidos para o `GET /admin/cupons`.

O cupom é criado no painel da Stripe (passo a passo em
07-tecnico/stack-e-deploy.md, no vault); o backend só lista os válidos e os
aplica. Cache em memória, por processo, de 1 minuto, no padrão de
app/services/assinatura/precos.py: o cupom novo aparece em até 1 minuto. Com o
cache vencido e a Stripe fora do ar, serve o último valor (e loga), sem tentar de
novo por 30 segundos; sem nenhum valor guardado, 502. Sem a cobrança ligada,
503, e nada é chamado. Só os `CUPONS_POR_CHAMADA` mais recentes da Stripe.

O resumo do desconto ("20% por 3 meses") sai daqui e é o que o histórico e o
e-mail mostram; é copiado para `cupons_enviados`.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import stripe

from app.core.plano import agora
from app.services.assinatura import stripe_api

logger = logging.getLogger(__name__)

VALIDADE_DO_CACHE = timedelta(minutes=1)
ESPERA_APOS_ERRO = timedelta(seconds=30)


@dataclass
class _Cache:
    cupons: list[dict] | None = None
    valido_ate: datetime | None = None
    nao_tentar_antes: datetime | None = None


_cache = _Cache()
_trava = threading.Lock()


def limpar_cache() -> None:
    global _cache
    with _trava:
        _cache = _Cache()


def _valor(centavos: int, moeda: str) -> str:
    if moeda.lower() == "brl":
        inteiro, fracao = divmod(centavos, 100)
        return f"R$ {inteiro:,}".replace(",", ".") + f",{fracao:02d}"
    return f"{centavos / 100:.2f} {moeda.upper()}"


def resumo_do_desconto(cupom: dict) -> str:
    """ "20% por 3 meses", "R$ 50,00 em uma cobrança", "10% para sempre"."""
    if cupom.get("percent_off") is not None:
        percentual = float(cupom["percent_off"])
        quanto = f"{percentual:g}%".replace(".", ",")
    else:
        quanto = _valor(cupom.get("amount_off") or 0, cupom.get("currency") or "brl")
    duracao = cupom.get("duration")
    if duracao == "forever":
        return f"{quanto} para sempre"
    if duracao == "repeating":
        meses = cupom.get("duration_in_months") or 0
        return f"{quanto} por {meses} {'mês' if meses == 1 else 'meses'}"
    return f"{quanto} em uma cobrança"


def nome_do_cupom(cupom: dict) -> str:
    return cupom.get("name") or cupom["id"]


def cupom_valido(cupom: dict | None) -> bool:
    return cupom is not None and bool(cupom.get("valid"))


def _item(cupom: dict) -> dict:
    return {
        "id": cupom["id"],
        "nome": nome_do_cupom(cupom),
        "percent_off": cupom.get("percent_off"),
        "amount_off": cupom.get("amount_off"),
        "moeda": cupom.get("currency"),
        "duration": cupom["duration"],
        "duration_in_months": cupom.get("duration_in_months"),
        "resumo": resumo_do_desconto(cupom),
    }


def cupons_validos() -> list[dict]:
    """Os cupons válidos, do cache ou da Stripe. Levanta o 503 (cobrança
    desligada) ou o 502 (Stripe fora do ar e nada guardado)."""
    chave = stripe_api.exigir_stripe()
    instante = agora()
    with _trava:
        if _cache.cupons is not None:
            if _cache.valido_ate and instante < _cache.valido_ate:
                return _cache.cupons
            if _cache.nao_tentar_antes and instante < _cache.nao_tentar_antes:
                return _cache.cupons
        try:
            lista = [_item(c) for c in stripe_api.listar_cupons(chave) if cupom_valido(c)]
        except stripe.StripeError as exc:
            if _cache.cupons is None:
                raise stripe_api.erro_da_stripe(exc, "cupons") from exc
            logger.warning("cupons resultado=cache_vencido erro=%s", type(exc).__name__)
            _cache.nao_tentar_antes = instante + ESPERA_APOS_ERRO
            return _cache.cupons
        _cache.cupons = lista
        _cache.valido_ate = instante + VALIDADE_DO_CACHE
        _cache.nao_tentar_antes = None
        return lista
