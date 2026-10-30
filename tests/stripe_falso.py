"""A Stripe em memória para os testes (issues #166 e #167): troca, por
funções que gravam as chamadas, os métodos da biblioteca `stripe` que o app e o
script do catálogo usam. Nenhum teste fala com a Stripe de verdade.

As chamadas ficam em `chamadas` como `(nome, parametros)`, para os testes
conferirem o que foi enviado (inclusive `idempotency_key`, `api_key` e
`stripe_version`). `assinaturas` é o que `Subscription.retrieve` devolve,
indexado pelo id: o teste põe ali o estado da assinatura "na Stripe".
"""

import itertools
from types import SimpleNamespace

import stripe


def objeto(dados: dict):
    return stripe.StripeObject.construct_from(dados, "sk_test_falsa")


class StripeFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict]] = []
        self.produtos: dict[str, dict] = {}
        self.precos: dict[str, dict] = {}
        self.clientes: dict[str, dict] = {}
        self.sessoes: dict[str, dict] = {}
        self.assinaturas: dict[str, dict] = {}
        # Com isto, o Customer.create com tax id é recusado (CNPJ no formato novo).
        self.recusar_tax_id = False
        # Com isto, toda chamada levanta um erro da Stripe.
        self.erro: Exception | None = None
        self._contador = itertools.count(1)

    # --- ajudantes dos testes -------------------------------------------------

    def chamadas_de(self, nome: str) -> list[dict]:
        return [parametros for chamado, parametros in self.chamadas if chamado == nome]

    def nomes(self) -> list[str]:
        return [nome for nome, _ in self.chamadas]

    def _registrar(self, nome: str, parametros: dict) -> None:
        self.chamadas.append((nome, parametros))
        if self.erro is not None:
            raise self.erro

    def _id(self, prefixo: str) -> str:
        return f"{prefixo}_{next(self._contador)}"

    # --- métodos substituídos ---------------------------------------------------

    def price_list(self, **params):
        self._registrar("Price.list", params)
        chaves = params.get("lookup_keys") or []
        achados = [
            objeto(preco)
            for preco in self.precos.values()
            if preco["lookup_key"] in chaves and (not params.get("active") or preco["active"])
        ]
        return SimpleNamespace(data=achados[: params.get("limit", 10)])

    def price_create(self, **params):
        self._registrar("Price.create", params)
        chave = params.get("lookup_key")
        dono = next((p for p in self.precos.values() if p["lookup_key"] == chave and chave), None)
        if dono is not None:
            if not params.get("transfer_lookup_key"):
                raise stripe.InvalidRequestError("lookup_key já em uso", param="lookup_key")
            dono["lookup_key"] = None
        preco = {
            "id": self._id("price"),
            "object": "price",
            "active": True,
            "product": params["product"],
            "currency": params["currency"],
            "unit_amount": params["unit_amount"],
            "recurring": params["recurring"],
            "lookup_key": chave,
        }
        self.precos[preco["id"]] = preco
        return objeto(preco)

    def price_modify(self, id, **params):
        self._registrar("Price.modify", {"id": id, **params})
        self.precos[id].update({k: v for k, v in params.items() if k == "active"})
        return objeto(self.precos[id])

    def product_retrieve(self, id, **params):
        self._registrar("Product.retrieve", {"id": id, **params})
        if id not in self.produtos:
            raise stripe.InvalidRequestError("No such product", param="id", code="resource_missing")
        return objeto(self.produtos[id])

    def product_create(self, **params):
        self._registrar("Product.create", params)
        self.produtos[params["id"]] = {
            "id": params["id"],
            "object": "product",
            "name": params["name"],
            "active": True,
            "metadata": params.get("metadata", {}),
        }
        return objeto(self.produtos[params["id"]])

    def product_modify(self, id, **params):
        self._registrar("Product.modify", {"id": id, **params})
        self.produtos[id].update({k: v for k, v in params.items() if k in ("name", "active")})
        return objeto(self.produtos[id])

    def customer_create(self, **params):
        self._registrar("Customer.create", params)
        if params.get("tax_id_data") and self.recusar_tax_id:
            raise stripe.InvalidRequestError("Invalid tax id", param="tax_id_data")
        cliente = {"id": self._id("cus"), "object": "customer", **params}
        self.clientes[cliente["id"]] = cliente
        return objeto({"id": cliente["id"], "object": "customer"})

    def session_create(self, **params):
        self._registrar("checkout.Session.create", params)
        sessao_id = self._id("cs_test")
        sessao = {"id": sessao_id, "object": "checkout.session", **params}
        self.sessoes[sessao_id] = sessao
        return objeto({**sessao, "url": f"https://checkout.stripe.com/c/pay/{sessao_id}"})

    def subscription_retrieve(self, id, **params):
        self._registrar("Subscription.retrieve", {"id": id, **params})
        if id not in self.assinaturas:
            raise stripe.InvalidRequestError(
                "No such subscription", param="id", code="resource_missing"
            )
        return objeto(self.assinaturas[id])

    def instalar(self, monkeypatch) -> None:
        for classe, nome, funcao in (
            (stripe.Price, "list", self.price_list),
            (stripe.Price, "create", self.price_create),
            (stripe.Price, "modify", self.price_modify),
            (stripe.Product, "retrieve", self.product_retrieve),
            (stripe.Product, "create", self.product_create),
            (stripe.Product, "modify", self.product_modify),
            (stripe.Customer, "create", self.customer_create),
            (stripe.checkout.Session, "create", self.session_create),
            (stripe.Subscription, "retrieve", self.subscription_retrieve),
        ):
            monkeypatch.setattr(classe, nome, staticmethod(funcao))


def assinatura_da_stripe(
    subscription_id: str,
    *,
    conta_id,
    cliente_id: str,
    status: str = "active",
    lookup_key: str | None = "padrao_mensal",
    fim_do_periodo: int = 1_800_000_000,
    cancelar_no_fim: bool = False,
    cancelada_em: int | None = None,
    metadata: bool = True,
) -> dict:
    """A Subscription como a API de 2026-10-28 a devolve: o fim do período fica
    no item, não na assinatura."""
    return {
        "id": subscription_id,
        "object": "subscription",
        "status": status,
        "customer": cliente_id,
        "metadata": {"conta_id": str(conta_id)} if metadata else {},
        "cancel_at_period_end": cancelar_no_fim,
        "canceled_at": cancelada_em,
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_1",
                    "current_period_end": fim_do_periodo,
                    "price": {"id": "price_1", "lookup_key": lookup_key},
                }
            ],
        },
    }
