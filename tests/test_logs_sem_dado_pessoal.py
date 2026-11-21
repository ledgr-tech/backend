"""Guarda da regra da issue #34 em todo log de `app/`.

A regra (docstring de `app/core/logging.py`): nunca e-mail, nome, razão social,
CNPJ, descrição de lançamento, token ou link no log. Loga-se identificador
(UUID), motivo, código de status e o nome da classe da exceção, nunca a
mensagem dela.

Até aqui a regra dependia de revisão. Este teste lê o código de `app/` (AST) e
falha quando uma chamada de log:

- usa `logger.exception`, `exc_info` ou `stack_info` (o traceback leva a
  mensagem da exceção, e a do banco traz o DETAIL com o dado);
- monta a mensagem fora do texto fixo (f-string, `%` ou variável): o valor
  some da vista do teste e da revisão;
- passa a exceção inteira, `str(exc)`, `repr(exc)` ou `exc.args` (a exceção
  vai por `type(exc).__name__`, por atributos dela como `exc.motivo`, ou por
  `resumo_do_erro(exc)`);
- passa um valor com nome de dado pessoal ou segredo (`usuario.email`,
  `empresa.razao_social`, `cnpj`, `token`...).

Os nomes são conferidos pelo último nome do valor: `link.id` é um UUID e
passa; `link` sozinho não passa. Um nome novo de dado pessoal entra em
`NOMES_PROIBIDOS`.
"""

import ast
import pathlib
import textwrap

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
NIVEIS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
NOMES_PROIBIDOS = {
    "email",
    "nome",
    "razao_social",
    "cnpj",
    "cpf",
    "descricao",
    "token",
    "senha",
    "link",
    "url",
    "texto",
    "pergunta",
    "resposta",
    "prompt",
    "conteudo",
    "justificativa",
}
FUNCOES_DE_TEXTO = {"str", "repr", "ascii", "format"}
# `len` devolve só o tamanho; `resumo_do_erro` (app/core/logging.py) é o jeito
# de logar uma exceção: classe, causa e SQLSTATE, sem a mensagem.
FUNCOES_SEGURAS = {"len", "resumo_do_erro"}


def _e_chamada_de_log(no: ast.AST) -> bool:
    return (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr in NIVEIS
        and "log" in ast.unparse(no.func.value).lower()
    )


def _problemas_do_valor(no: ast.AST, excecoes: set[str]) -> list[str]:
    if isinstance(no, ast.Constant):
        return [f"texto {no.value!r} com nome de dado"] if no.value in NOMES_PROIBIDOS else []
    if isinstance(no, ast.Name):
        if no.id in excecoes:
            return [f"a exceção `{no.id}` inteira (leva a mensagem)"]
        return [f"`{no.id}` é dado pessoal ou segredo"] if no.id in NOMES_PROIBIDOS else []
    if isinstance(no, ast.Attribute):
        if no.attr == "args":
            return [f"`{ast.unparse(no)}` é a mensagem da exceção"]
        if no.attr in NOMES_PROIBIDOS:
            return [f"`{ast.unparse(no)}` é dado pessoal ou segredo"]
        return []
    if isinstance(no, ast.JoinedStr):
        return [f"f-string no valor ({ast.unparse(no)})"]
    if isinstance(no, ast.IfExp):
        return _problemas_do_valor(no.body, excecoes) + _problemas_do_valor(no.orelse, excecoes)
    if isinstance(no, ast.Call):
        if isinstance(no.func, ast.Name) and no.func.id in FUNCOES_SEGURAS:
            return []
        problemas = []
        if isinstance(no.func, ast.Name) and no.func.id in FUNCOES_DE_TEXTO:
            problemas += [
                f"`{ast.unparse(no)}` é a mensagem da exceção"
                for arg in no.args
                if isinstance(arg, ast.Name) and arg.id in excecoes
            ]
        for arg in [*no.args, *(kw.value for kw in no.keywords)]:
            problemas += _problemas_do_valor(arg, excecoes)
        return problemas
    return [
        problema
        for filho in ast.iter_child_nodes(no)
        for problema in _problemas_do_valor(filho, excecoes)
    ]


def problemas_de_log(fonte: str) -> list[tuple[int, str]]:
    """(linha, motivo) de cada chamada de log fora da regra."""
    arvore = ast.parse(fonte)
    excecoes = {no.name for no in ast.walk(arvore) if isinstance(no, ast.ExceptHandler) and no.name}
    problemas = []
    for chamada in filter(_e_chamada_de_log, ast.walk(arvore)):
        linha = chamada.lineno
        if chamada.func.attr == "exception":
            problemas.append((linha, "`logger.exception` leva o traceback"))
        problemas += [
            (linha, f"`{kw.arg}` leva o traceback")
            for kw in chamada.keywords
            if kw.arg in {"exc_info", "stack_info"}
        ]
        args = chamada.args[1:] if chamada.func.attr == "log" else chamada.args
        if not args:
            continue
        mensagem, *valores = args
        if not (isinstance(mensagem, ast.Constant) and isinstance(mensagem.value, str)):
            problemas.append((linha, "a mensagem não é um texto fixo"))
        for valor in valores:
            problemas += [(linha, motivo) for motivo in _problemas_do_valor(valor, excecoes)]
    return problemas


def _problemas(chamada: str) -> list[tuple[int, str]]:
    """A chamada dentro de um `except ... as exc`, como nas rotas."""
    corpo = textwrap.indent(chamada, " " * 4)
    return problemas_de_log(f"try:\n    pass\nexcept Exception as exc:\n{corpo}\n")


@pytest.mark.parametrize(
    "chamada",
    [
        'logger.info("login email=%s", usuario.email)',
        'logger.info("empresa=%s", empresa.razao_social)',
        'logger.info("cnpj=%s", cnpj)',
        'logger.info("linha=%s", lancamento.descricao)',
        'logger.info("token=%s", token)',
        'logger.info("destino=%s", normalizar_email(dados.email))',
        'logger.info("campo=%s", corpo.get("email"))',
        'logger.info("nome=%s", conta.nome if conta else None)',
        'logger.info("falhou %s", exc)',
        'logger.info("falhou %s", str(exc))',
        'logger.info("falhou %r", repr(exc))',
        'logger.info("falhou %s", exc.args[0])',
        'logger.info("falhou %s", formatar(exc))',
        'logger.info(f"falhou {usuario.id}")',
        'logger.info("falhou %s" % usuario.id)',
        'logger.info("falhou %s", f"{usuario.id}")',
        'logger.exception("falhou")',
        'logger.error("falhou", exc_info=True)',
        'logger.warning("falhou", stack_info=True)',
        'logger.log(logging.INFO, "email=%s", usuario.email)',
    ],
)
def test_guarda_recusa_log_com_dado_ou_com_a_mensagem_da_excecao(chamada):
    assert _problemas(chamada) != []


@pytest.mark.parametrize(
    "chamada",
    [
        'logger.info("falhou classe=%s", type(exc).__name__)',
        'logger.error("falhou %s", resumo_do_erro(exc))',
        'logger.error("grande_demais caracteres=%d", len(texto))',
        'logger.info("falhou motivo=%s status_http=%s", exc.motivo, exc.status_http)',
        'logger.info("link resultado=criado link_id=%s prefixo=%s", link.id, link.prefixo)',
        'logger.info("cupom email_enviado=%s", registro.email_enviado)',
        'logger.info("assistente links=%d", len(links))',
        'logger.info("api chave_id=%s", chave.chave_id if chave else None)',
        'logger.info("oauth resultado=%s", resposta.corpo.get("error"))',
        'logger.log(nivel, "%s motivo=%s", origem, exc.motivo)',
        'logger.info("duracao_ms=%s", round((time.monotonic() - inicio) * 1000))',
        'logger.info("desligado")',
    ],
)
def test_guarda_aceita_identificador_motivo_e_classe(chamada):
    assert _problemas(chamada) == []


def test_todo_log_de_app_segue_a_regra():
    achados = [
        f"{arquivo.relative_to(RAIZ)}:{linha}: {motivo}"
        for arquivo in sorted((RAIZ / "app").rglob("*.py"))
        for linha, motivo in problemas_de_log(arquivo.read_text())
    ]

    assert achados == [], "\n".join(achados)
