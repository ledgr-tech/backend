"""Os preços públicos dos planos (issues #166 e #167, ADR-026): o que a Stripe
tem no catálogo, repassado pelo `GET /assinatura/precos`.

O backend não guarda preço: busca na Stripe os preços ativos das 12
`lookup_keys` "<plano>_<periodo>" e devolve uma lista ordenada por plano
(`PLANOS`) e período (`PERIODOS`). Chave ausente no catálogo não aparece.

Cache em memória, por processo, de 10 minutos, para não chamar a Stripe a cada
visita: depois de rodar scripts/stripe_catalogo.py, o preço novo aparece em até
10 minutos (ou no próximo deploy). Com o cache vencido e a Stripe fora do ar,
serve o último valor (e loga), sem tentar de novo por 1 minuto; sem nenhum valor
guardado, 502. Sem a cobrança ligada, 503, e nada é chamado.

Limites do plano (issue #183): cada item traz o bloco `limites` do seu plano
(conciliações por mês, empresas e usuários; `None` é sem limite), o mesmo em
qualquer período, pela mesma regra do 402 e do `GET /me` (`limite_do_plano`,
app/core/plano.py). O cache guarda só o que vem da Stripe: os limites não
entram nele e são lidos da configuração a cada chamada (`precos_com_limites`).
Isso não os deixa mais "frescos" que os preços, só de outro jeito: a
configuração (`LIMITE_<RECURSO>_<PLANO>`) é lida uma vez, quando o processo
sobe, então mudar um limite exige reiniciar o serviço, e a resposta passa a
trazê-lo na hora, sem esperar o cache. Já o preço novo leva até 10 minutos.
Com as variáveis vazias, tudo é `None`: nenhum valor padrão mora no backend.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import stripe

from app.core.plano import PERIODOS, PLANOS, Plano, agora, limite_do_plano
from app.services.assinatura import stripe_api

logger = logging.getLogger(__name__)

VALIDADE_DO_CACHE = timedelta(minutes=10)
# Com o cache vencido e a Stripe falhando, não insiste a cada visita.
ESPERA_APOS_ERRO = timedelta(minutes=1)


@dataclass
class _Cache:
    precos: list[dict] | None = None
    valido_ate: datetime | None = None
    nao_tentar_antes: datetime | None = None


_cache = _Cache()
_trava = threading.Lock()


def limpar_cache() -> None:
    global _cache
    with _trava:
        _cache = _Cache()


def _lookups() -> list[str]:
    return [stripe_api.lookup_key(plano, periodo) for plano in PLANOS for periodo in PERIODOS]


def _montar(precos_da_stripe: list[dict]) -> list[dict]:
    por_chave = {preco.get("lookup_key"): preco for preco in precos_da_stripe}
    lista = []
    for plano in PLANOS:
        for periodo in PERIODOS:
            preco = por_chave.get(stripe_api.lookup_key(plano, periodo))
            if preco is None:
                continue
            recorrencia = preco.get("recurring") or {}
            produto = preco.get("product")
            lista.append(
                {
                    "plano": plano,
                    "periodo": periodo,
                    "valor_centavos": preco["unit_amount"],
                    "moeda": preco["currency"],
                    "intervalo": recorrencia.get("interval"),
                    "quantidade_de_intervalos": recorrencia.get("interval_count"),
                    "nome_do_produto": produto.get("name") if isinstance(produto, dict) else None,
                }
            )
    return lista


def precos_publicos() -> list[dict]:
    """A lista de preços, do cache ou da Stripe. Levanta o 503 (cobrança
    desligada) ou o 502 (Stripe fora do ar e nada guardado)."""
    chave = stripe_api.exigir_stripe()
    instante = agora()
    with _trava:
        if _cache.precos is not None:
            if _cache.valido_ate and instante < _cache.valido_ate:
                return _cache.precos
            if _cache.nao_tentar_antes and instante < _cache.nao_tentar_antes:
                return _cache.precos
        try:
            lista = _montar(stripe_api.listar_precos(chave, _lookups()))
        except stripe.StripeError as exc:
            if _cache.precos is None:
                raise stripe_api.erro_da_stripe(exc, "precos") from exc
            logger.warning("precos resultado=cache_vencido erro=%s", type(exc).__name__)
            _cache.nao_tentar_antes = instante + ESPERA_APOS_ERRO
            return _cache.precos
        _cache.precos = lista
        _cache.valido_ate = instante + VALIDADE_DO_CACHE
        _cache.nao_tentar_antes = None
        return lista


def limites_do_plano(plano: Plano) -> dict[str, int | None]:
    """O bloco `limites` do plano, lido da configuração (nunca do cache)."""
    return {
        "conciliacoes_por_mes": limite_do_plano(plano, "conciliacoes"),
        "empresas": limite_do_plano(plano, "empresas"),
        "usuarios": limite_do_plano(plano, "usuarios"),
    }


def precos_com_limites() -> list[dict]:
    """`precos_publicos` com o bloco `limites` de cada plano em cada item. Monta
    dicts novos: o que está no cache não é alterado."""
    return [{**preco, "limites": limites_do_plano(preco["plano"])} for preco in precos_publicos()]
