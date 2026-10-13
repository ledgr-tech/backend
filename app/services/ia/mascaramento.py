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

Modo "termos" (issue #87, `LLM_MODO_DESCRICAO`, padrão enquanto não houver
Zero Data Retention): depois da máscara, `filtrar_termos` deixa passar só as
palavras de `TERMOS_PERMITIDOS` e os marcadores acima; qualquer outra palavra
(nome de pessoa, de empresa, número curto) vira `[TEXTO]`, e `[TEXTO]`
seguidos viram um só. É lista fechada de propósito: o que não está nela não
sai, em vez de tentar adivinhar o que é nome. O modo "mascarada" é o
comportamento anterior, só a máscara.

Não reaproveita `TERMOS_TARIFA_BANCARIA` de app/services/matching.py: lá são
expressões ("manutencao de conta"), comparadas por substring, e importar o
motor puxaria os models para esta camada, que não toca banco (ver
tests/test_ia_config.py). As palavras de lá que fazem sentido sozinhas
(TARIFA, TAR, PACOTE, MANUTENCAO, CONTA, CESTA, SERVICOS, ANUIDADE, IOF e
TAXA) estão na lista abaixo.
"""

import re
import unicodedata
from typing import Literal

ModoDescricao = Literal["termos", "mascarada"]
MODO_TERMOS: ModoDescricao = "termos"
MODO_MASCARADA: ModoDescricao = "mascarada"

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


# Termos bancários e de operação que podem chegar ao provedor no modo
# "termos" (issue #87). Comparados sem acento e em maiúsculas.
TERMOS_PERMITIDOS = frozenset(
    {
        "PIX", "TED", "DOC", "TEF", "BOLETO", "PAG", "PAGTO", "PAGAMENTO",
        "TARIFA", "TAR", "TAXA", "IOF", "JUROS", "MULTA", "ESTORNO", "DEVOLUCAO",
        "SAQUE", "DEPOSITO", "TRANSF", "TRANSFERENCIA", "RECEBIMENTO", "ENVIADO",
        "RECEBIDO", "CARTAO", "DEBITO", "CREDITO", "DEB", "CRED", "AUTOMATICO",
        "CONVENIO", "FOLHA", "SALARIO", "ALUGUEL", "IMPOSTO", "DARF", "GPS", "FGTS",
        "DAS", "SIMPLES", "CONTA", "ENERGIA", "AGUA", "MENSALIDADE", "PACOTE",
        "SERVICOS", "CESTA", "MANUTENCAO", "ANUIDADE", "RENDIMENTO", "APLICACAO",
        "RESGATE", "CHEQUE", "COMPENSADO",
    }
)  # fmt: skip

MARCADOR_TEXTO = "[TEXTO]"
_MARCADORES = frozenset({"[CPF]", "[CNPJ]", "[EMAIL]", "[TELEFONE]", "[NUMERO]", MARCADOR_TEXTO})
# Um marcador ou uma palavra (letras e dígitos, com acento). Pontuação e
# espaço ficam de fora e somem da saída.
_PADRAO_PALAVRA = re.compile(r"\[[A-Z]+\]|[^\W_]+")


def _sem_acento_maiuscula(palavra: str) -> str:
    decomposta = unicodedata.normalize("NFKD", palavra)
    return "".join(c for c in decomposta if not unicodedata.combining(c)).upper()


def filtrar_termos(texto: str) -> str:
    """Deixa passar, como vieram, só as palavras de `TERMOS_PERMITIDOS` e os
    marcadores da máscara; o resto vira `[TEXTO]`, sem repetir em seguida.
    As palavras saem separadas por um espaço. Recebe o texto JÁ mascarado:
    sem a máscara, um CPF virava `[TEXTO]` em vez de `[CPF]`. Determinística
    e idempotente."""
    saida: list[str] = []
    for encontrado in _PADRAO_PALAVRA.finditer(texto):
        palavra = encontrado.group(0)
        if palavra in _MARCADORES or _sem_acento_maiuscula(palavra) in TERMOS_PERMITIDOS:
            token = palavra
        else:
            token = MARCADOR_TEXTO
        if token == MARCADOR_TEXTO and saida and saida[-1] == MARCADOR_TEXTO:
            continue
        saida.append(token)
    return " ".join(saida)


def preparar_descricao(texto: str, modo: ModoDescricao) -> str:
    """O que sai da descrição para o provedor: a máscara sempre, e no modo
    "termos" também `filtrar_termos`. Modo desconhecido levanta `ValueError`
    (a configuração já recusa no boot; aqui é a última barreira)."""
    mascarado = mascarar_descricao(texto)
    if modo == MODO_MASCARADA:
        return mascarado
    if modo == MODO_TERMOS:
        return filtrar_termos(mascarado)
    raise ValueError(f"Modo de descrição desconhecido: {modo!r}")
