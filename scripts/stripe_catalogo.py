"""Cria e mantém o catálogo da Stripe: um Product por plano e um Price
recorrente por plano e período (issues #166 e #167, ADR-026).

Os valores moram em scripts/stripe_precos.json (versionado, em centavos, hoje
provisórios): nenhum preço mora no backend, que acha o preço pela `lookup_key`
"<plano>_<periodo>", por exemplo "padrao_trimestral".

- Product: um por plano, com o id fixo `ledgr_<plano>` e a metadata `plano`.
  Id fixo, e não busca, para a segunda execução achar o que a primeira criou
  na hora (a busca da Stripe demora a refletir o que foi criado).
- Price: mensal (month, 1), trimestral (month, 3), semestral (month, 6) e anual
  (year, 1), com `lookup_key` no formato acima.
- Idempotente: rodar duas vezes sem mudar o JSON não cria nada.
- O Price é imutável na Stripe: se o valor do JSON mudar, cria um Price novo com
  `transfer_lookup_key=True` (a chave passa para o novo) e arquiva o antigo. As
  assinaturas que já usam o preço antigo continuam nele.
- `--dry-run` só mostra o que faria, sem chamar nenhuma criação nem alteração.

A chave é a `STRIPE_SECRET_KEY` do ambiente (.env), e ela define o modo: a mesma
execução vale em teste e em live. Com chave live, `Settings` já recusa o boot
sem `STRIPE_MODO_LIVE=true`. Mostra o modo antes de começar. A API é a versão
fixada em app/services/assinatura/stripe_api.py.

Uso, no diretório do backend:

    ./venv/bin/python scripts/stripe_catalogo.py --dry-run
    ./venv/bin/python scripts/stripe_catalogo.py
"""

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/stripe_catalogo.py`, o sys.path recebe scripts/,
# não a raiz do repositório (mesmo caso de scripts/ativar_empresa.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import stripe

from app.core.config import settings
from app.core.plano import PERIODOS, PLANOS
from app.services.assinatura.stripe_api import API_VERSION, lookup_key

ARQUIVO_DE_PRECOS = Path(__file__).resolve().parent / "stripe_precos.json"
# periodo -> (interval, interval_count) do Price recorrente
RECORRENCIA = {
    "mensal": ("month", 1),
    "trimestral": ("month", 3),
    "semestral": ("month", 6),
    "anual": ("year", 1),
}


def _id_do_produto(plano: str) -> str:
    return f"ledgr_{plano}"


def carregar_precos(arquivo: Path) -> dict:
    """O JSON de preços, conferido: todos os planos e períodos, valores inteiros
    positivos em centavos."""
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    moeda = dados["moeda"].lower()
    planos = dados["planos"]
    for plano in PLANOS:
        for periodo in PERIODOS:
            valor = planos[plano][periodo]
            if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
                raise ValueError(f"valor inválido em {plano}/{periodo}: use centavos inteiros")
    return {"moeda": moeda, "planos": planos}


def _opcoes(chave: str) -> dict:
    return {"api_key": chave, "stripe_version": API_VERSION}


def _produto(chave: str, plano: str, nome: str, dry_run: bool, log: list[str]) -> None:
    id_ = _id_do_produto(plano)
    try:
        existente = stripe.Product.retrieve(id_, **_opcoes(chave))
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) != "resource_missing":
            raise
        existente = None
    if existente is None:
        log.append(f"produto {id_}: criar")
        if not dry_run:
            stripe.Product.create(id=id_, name=nome, metadata={"plano": plano}, **_opcoes(chave))
        return
    if existente.name != nome or not existente.active:
        log.append(f"produto {id_}: atualizar")
        if not dry_run:
            stripe.Product.modify(id_, name=nome, active=True, **_opcoes(chave))
        return
    log.append(f"produto {id_}: ok")


def _preco_confere(preco, produto_id: str, moeda: str, valor: int, periodo: str) -> bool:
    intervalo, quantidade = RECORRENCIA[periodo]
    recorrencia = preco.recurring
    return (
        preco.unit_amount == valor
        and preco.currency == moeda
        and preco.product == produto_id
        and recorrencia is not None
        and recorrencia.interval == intervalo
        and recorrencia.interval_count == quantidade
    )


def _preco(
    chave: str, plano: str, periodo: str, moeda: str, valor: int, dry_run: bool, log: list[str]
) -> None:
    lookup = lookup_key(plano, periodo)
    produto_id = _id_do_produto(plano)
    ativos = stripe.Price.list(lookup_keys=[lookup], active=True, limit=1, **_opcoes(chave)).data
    atual = ativos[0] if ativos else None
    if atual is not None and _preco_confere(atual, produto_id, moeda, valor, periodo):
        log.append(f"preço {lookup}: ok")
        return
    intervalo, quantidade = RECORRENCIA[periodo]
    log.append(
        f"preço {lookup}: {'trocar o valor (novo Price, chave transferida)' if atual else 'criar'}"
    )
    if dry_run:
        return
    stripe.Price.create(
        product=produto_id,
        currency=moeda,
        unit_amount=valor,
        recurring={"interval": intervalo, "interval_count": quantidade},
        lookup_key=lookup,
        transfer_lookup_key=True,
        metadata={"plano": plano, "periodo": periodo},
        **_opcoes(chave),
    )
    if atual is not None:
        stripe.Price.modify(atual.id, active=False, **_opcoes(chave))
        log.append(f"preço {lookup}: Price antigo arquivado")


def sincronizar_catalogo(chave: str, precos: dict, dry_run: bool) -> list[str]:
    """Leva a Stripe ao que está no JSON e devolve o que fez (ou faria)."""
    log: list[str] = []
    for plano in PLANOS:
        dados = precos["planos"][plano]
        _produto(chave, plano, dados["nome"], dry_run, log)
        for periodo in PERIODOS:
            _preco(chave, plano, periodo, precos["moeda"], dados[periodo], dry_run, log)
    return log


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cria e mantém os produtos e preços da Stripe (issues #166 e #167)."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="só mostra o que faria, sem criar nem alterar"
    )
    parser.add_argument(
        "--arquivo",
        type=Path,
        default=ARQUIVO_DE_PRECOS,
        help="o JSON de preços (padrão: scripts/stripe_precos.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    chave = settings.stripe_secret_key.strip()
    if not chave:
        print("STRIPE_SECRET_KEY não está no ambiente. Nada foi feito.")
        return 1
    precos = carregar_precos(args.arquivo)
    modo = "live" if chave.startswith(("sk_live_", "rk_live_")) else "teste"
    print(f"Modo da chave: {modo}{' (simulação)' if args.dry_run else ''}")
    for linha in sincronizar_catalogo(chave, precos, args.dry_run):
        print(linha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
