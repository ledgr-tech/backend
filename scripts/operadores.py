"""Inclui, remove e lista os operadores do Ledgr (issue #156, ADR-025).

O operador acessa o painel `/admin`. É ligado a um usuário que já existe
(entrou pelo cadastro ou por convite) e não é um papel da conta: nenhuma
rota cria nem remove operador, só este script. A remoção vale na próxima
requisição do operador ao painel.

Uso, no diretório do backend:

    ./venv/bin/python scripts/operadores.py --listar
    ./venv/bin/python scripts/operadores.py --incluir <e-mail do usuário> --por "<seu nome>"
    ./venv/bin/python scripts/operadores.py --remover <e-mail do usuário> --por "<seu nome>"

Incluir e remover mostram o usuário (e-mail mascarado, id e a razão social
da conta dele) e pedem o e-mail completo digitado como confirmação. `--por`
é o nome de quem rodou, gravado em `incluido_por` ou `removido_por`.

Banco: a mesma guarda de scripts/excluir_empresa.py. Por padrão, recusa
qualquer banco que não seja local; banco remoto só com `--banco-remoto` e o
host digitado como segunda confirmação.

Nunca imprime o e-mail inteiro nem o nome do usuário.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/operadores.py`, o sys.path recebe scripts/, não a
# raiz do repositório (mesmo caso de scripts/excluir_empresa.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.auth import normalizar_email
from app.core.config import settings
from app.models import Conta, OperadorLedgr, Usuario
from scripts.excluir_empresa import _banco_liberado


def _criar_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": 10})


def email_mascarado(email: str) -> str:
    """`ana@ledgr.com.br` vira `a**@ledgr.com.br`."""
    nome, _, dominio = email.partition("@")
    return f"{nome[:1]}{'*' * max(len(nome) - 1, 2)}@{dominio}"


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inclui, remove e lista os operadores do Ledgr (issue #156)."
    )
    acao = parser.add_mutually_exclusive_group(required=True)
    acao.add_argument("--listar", action="store_true", help="lista os operadores ativos")
    acao.add_argument("--incluir", metavar="EMAIL", help="e-mail do usuário a incluir")
    acao.add_argument("--remover", metavar="EMAIL", help="e-mail do operador a remover")
    parser.add_argument("--por", help="seu nome, gravado como quem incluiu ou removeu")
    parser.add_argument(
        "--banco-remoto",
        action="store_true",
        help="aceita um DATABASE_URL que não seja local, depois de pedir o host digitado",
    )
    args = parser.parse_args(argv)
    if (args.incluir or args.remover) and not (args.por or "").strip():
        parser.error("--incluir e --remover pedem --por com o seu nome")
    return args


def _usuario(db: Session, email: str) -> Usuario | None:
    return db.scalar(
        select(Usuario).where(
            Usuario.email == normalizar_email(email), Usuario.removido_em.is_(None)
        )
    )


def _ativo(db: Session, usuario: Usuario) -> OperadorLedgr | None:
    return db.scalar(
        select(OperadorLedgr).where(
            OperadorLedgr.usuario_id == usuario.id, OperadorLedgr.removido_em.is_(None)
        )
    )


def listar(db: Session) -> None:
    linhas = db.execute(
        select(OperadorLedgr, Usuario.email, Conta.razao_social)
        .join(Usuario, Usuario.id == OperadorLedgr.usuario_id)
        .join(Conta, Conta.id == Usuario.conta_id)
        .where(OperadorLedgr.removido_em.is_(None))
        .order_by(OperadorLedgr.incluido_em)
    ).all()
    if not linhas:
        print("Nenhum operador ativo.")
        return
    for operador, email, razao_social in linhas:
        print(
            f"{email_mascarado(email)}  usuario {operador.usuario_id}  conta {razao_social}  "
            f"incluído em {operador.incluido_em:%d/%m/%Y} por {operador.incluido_por}"
        )


def _confirmar(db: Session, usuario: Usuario, acao: str, perguntar: Callable[[str], str]) -> bool:
    conta = db.get(Conta, usuario.conta_id)
    print(f"Usuário:  {email_mascarado(usuario.email)} ({usuario.id})")
    print(f"Conta:    {conta.razao_social}")
    print(f"Ação:     {acao}")
    digitado = normalizar_email(perguntar("Digite o e-mail completo do usuário: "))
    if digitado != usuario.email:
        print("O e-mail digitado não confere. Nada foi gravado.")
        return False
    return True


def incluir(db: Session, email: str, por: str, perguntar: Callable[[str], str]) -> int:
    usuario = _usuario(db, email)
    if usuario is None:
        print("Usuário não encontrado (ou removido). Nada foi gravado.")
        return 1
    if _ativo(db, usuario) is not None:
        print("Este usuário já é operador. Nada foi gravado.")
        return 1
    if not _confirmar(db, usuario, "incluir como operador do Ledgr", perguntar):
        return 1
    db.add(OperadorLedgr(usuario_id=usuario.id, incluido_por=por.strip()[:100]))
    db.commit()
    print("Gravado. O usuário já pode usar o /admin (com login de no máximo 2 horas).")
    return 0


def remover(db: Session, email: str, por: str, perguntar: Callable[[str], str]) -> int:
    usuario = _usuario(db, email)
    operador = _ativo(db, usuario) if usuario is not None else None
    if operador is None:
        print("Este e-mail não é de um operador ativo. Nada foi gravado.")
        return 1
    if not _confirmar(db, usuario, "remover dos operadores do Ledgr", perguntar):
        return 1
    operador.removido_em = func.now()
    operador.removido_por = por.strip()[:100]
    db.commit()
    print("Gravado. O acesso ao /admin acaba na próxima requisição.")
    return 0


def main(argv: list[str] | None = None, perguntar: Callable[[str], str] = input) -> int:
    args = _argumentos(argv)
    url = settings.database_url
    if not _banco_liberado(url, args.banco_remoto, perguntar):
        return 1
    destino = make_url(url)
    print(f"Banco:    {destino.host}/{destino.database}")
    engine = _criar_engine(url)
    try:
        with Session(engine) as db:
            if args.listar:
                listar(db)
                return 0
            if args.incluir:
                return incluir(db, args.incluir, args.por, perguntar)
            return remover(db, args.remover, args.por, perguntar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
