"""Cidade e UF da conta (issue #184).

O dado é da conta (`contas.cidade` e `contas.uf`), não da empresa cliente: serve
para saber onde estão os clientes do Ledgr. As regras são as mesmas no cadastro
(`/register` e `/register/google`) e no que vem da consulta de CNPJ:

- vazio ou só espaços vale como não informado (`None`), em cidade e em UF;
- cidade: sem espaços nas pontas, de 2 a 80 caracteres, a caixa como veio;
- UF: sem espaços nas pontas, em maiúsculas, uma das 27 siglas.

Os mesmos limites estão nos CHECKs da tabela (migrations/versions/
b9e3c7a1d548_cidade_e_uf_da_conta_issue_184.py).
"""

UFS = (
    "AC",
    "AL",
    "AM",
    "AP",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MG",
    "MS",
    "MT",
    "PA",
    "PB",
    "PE",
    "PI",
    "PR",
    "RJ",
    "RN",
    "RO",
    "RR",
    "RS",
    "SC",
    "SE",
    "SP",
    "TO",
)
CIDADE_MINIMO = 2
CIDADE_MAXIMO = 80

# As mensagens não repetem o valor enviado: ele não pode ir parar em log nem em
# corpo de erro (app/core/erros_validacao.py tira o `input` do 422).
CIDADE_FORA_DO_TAMANHO = f"a cidade deve ter de {CIDADE_MINIMO} a {CIDADE_MAXIMO} caracteres"
UF_INVALIDA = "UF inválida: use a sigla de um dos 27 estados"


def normalizar_cidade(valor: str) -> str | None:
    """A cidade sem espaços nas pontas, `None` se vazia; `ValueError` se fora
    de 2 a 80 caracteres."""
    cidade = valor.strip()
    if not cidade:
        return None
    if not CIDADE_MINIMO <= len(cidade) <= CIDADE_MAXIMO:
        raise ValueError(CIDADE_FORA_DO_TAMANHO)
    return cidade


def normalizar_uf(valor: str) -> str | None:
    """A UF em maiúsculas, `None` se vazia; `ValueError` se não for uma das 27."""
    uf = valor.strip().upper()
    if not uf:
        return None
    if uf not in UFS:
        raise ValueError(UF_INVALIDA)
    return uf


def cidade_da_receita(valor: object) -> str | None:
    """O município que a consulta de CNPJ devolveu, ou `None` se não serve.
    Nunca levanta: dado estranho do provedor só deixa o campo sem pré-preencher."""
    if not isinstance(valor, str):
        return None
    try:
        return normalizar_cidade(valor)
    except ValueError:
        return None


def uf_da_receita(valor: object) -> str | None:
    """A UF que a consulta de CNPJ devolveu, ou `None` se não serve."""
    if not isinstance(valor, str):
        return None
    try:
        return normalizar_uf(valor)
    except ValueError:
        return None
