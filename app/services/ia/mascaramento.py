"""Mascaramento de dado sensível em descrição de lançamento, antes de
entrar no prompt da IA (issue #28, ADR-011).

Ordem de aplicação (ADR-011; cada passo roda sobre o resultado do anterior,
sem reconsiderar o que já foi marcado): e-mail -> `[EMAIL]`; CNPJ (com ou
sem pontuação) -> `[CNPJ]`; CPF (com ou sem pontuação, inclusive
parcialmente oculto, como `•••.123.456-••`) -> `[CPF]`; telefone brasileiro
(com ou sem DDD/+55, com ou sem hífen/parênteses) -> `[TELEFONE]`; qualquer
sequência restante de 6 ou mais dígitos (contando os separados só por
ponto, hífen ou espaço) -> `[NUMERO]`. Só depois do mascaramento o texto é
truncado em 120 caracteres.

Uma sequência crua de 11 dígitos sem nenhuma pontuação é ambígua entre CPF
e telefone (DDD + celular de 9 dígitos); como CPF vem antes na ordem acima,
esse caso vira `[CPF]` — só formato de telefone com parênteses, hífen ou
espaço (ou sem DDD, então não bate 11 dígitos exatos) escapa do passo de
CPF, que exige 11 dígitos consecutivos sem nenhuma pontuação no meio.

Determinístico e idempotente: nenhum marcador (`[EMAIL]`, `[CNPJ]`, ...)
contém dígito nem os caracteres que os padrões abaixo procuram, então
mascarar um texto já mascarado devolve o mesmo resultado.

Nomes de pessoa NÃO são mascarados (ADR-011): detectar nome por regra é
pouco confiável e daria falsa sensação de segurança. É uma limitação
conhecida, coberta por DPA/Zero Data Retention com a OpenAI e, depois,
pelo Sabiazinho BR-SP (mantém o dado em território nacional).
"""

import re

_TAMANHO_MAXIMO = 120

_PADRAO_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_PADRAO_CNPJ_PONTUADO = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_PADRAO_CNPJ_CRU = re.compile(r"(?<!\d)\d{14}(?!\d)")

# Cada grupo aceita dígitos OU a mesma quantidade de caracteres de
# ocultação (•/*), pra cobrir CPF parcialmente oculto (ex: "•••.123.456-••").
_GRUPO_CPF_3 = r"(?:\d{3}|[•*]{3})"
_GRUPO_CPF_2 = r"(?:\d{2}|[•*]{2})"
_PADRAO_CPF_PONTUADO = re.compile(
    rf"(?<![\d•*]){_GRUPO_CPF_3}\.{_GRUPO_CPF_3}\.{_GRUPO_CPF_3}-{_GRUPO_CPF_2}(?![\d•*])"
)
_PADRAO_CPF_CRU = re.compile(r"(?<!\d)\d{11}(?!\d)")

# +55 opcional, DDD opcional (com ou sem parênteses), número local de 8 ou
# 9 dígitos (celular com 9 na frente, fixo sem) com hífen opcional antes
# dos últimos 4 dígitos.
_PADRAO_TELEFONE = re.compile(
    r"(?<!\d)(?:\+?55[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?9?\d{4}-?\d{4}(?!\d)"
)

# Maximal: dígito, seguido de zero ou mais repetições de (separador opcional
# + dígito) — o callback abaixo decide se o total de dígitos bate o limiar.
_PADRAO_SEQUENCIA_DIGITOS = re.compile(r"\d(?:[.\-\s]?\d)*")
_LIMIAR_SEQUENCIA_DIGITOS = 6


def _substituir_numero_longo(encontrado: re.Match[str]) -> str:
    texto = encontrado.group(0)
    digitos = re.sub(r"\D", "", texto)
    return "[NUMERO]" if len(digitos) >= _LIMIAR_SEQUENCIA_DIGITOS else texto


def mascarar_descricao(texto: str) -> str:
    """Mascara e-mail, CNPJ, CPF, telefone e sequências longas de dígitos
    (nesta ordem, ver docstring do módulo) e trunca o resultado em 120
    caracteres. Determinística e idempotente; texto vazio devolve vazio."""
    mascarado = _PADRAO_EMAIL.sub("[EMAIL]", texto)
    mascarado = _PADRAO_CNPJ_PONTUADO.sub("[CNPJ]", mascarado)
    mascarado = _PADRAO_CNPJ_CRU.sub("[CNPJ]", mascarado)
    mascarado = _PADRAO_CPF_PONTUADO.sub("[CPF]", mascarado)
    mascarado = _PADRAO_CPF_CRU.sub("[CPF]", mascarado)
    mascarado = _PADRAO_TELEFONE.sub("[TELEFONE]", mascarado)
    mascarado = _PADRAO_SEQUENCIA_DIGITOS.sub(_substituir_numero_longo, mascarado)
    return mascarado[:_TAMANHO_MAXIMO]
