"""Cobrança pela Stripe (issues #166 e #167, ADR-026).

- `stripe_api`: as chamadas à Stripe (preço, cliente, checkout, assinatura) e a
  verificação da assinatura do webhook, com a versão da API fixada;
- `sincronizacao`: grava a assinatura e os pagamentos da conta a partir do que
  a Stripe devolve;
- `webhook`: o tratamento de cada tipo de evento, com idempotência;
- `cliente`: o Customer da conta, criado uma vez (checkout e cupom do `/admin`);
- `cupons`: os cupons da Stripe para o `/admin` (issue #169), com cache curto;
- `operador`: o que o operador faz na assinatura da conta pelo `/admin`
  (cancelar, manter e dar cupom), issue #169.

O webhook é a única fonte de confirmação do pagamento. O retorno do checkout
no navegador não libera nada.
"""
