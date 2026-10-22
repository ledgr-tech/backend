"""Lista as perguntas que a Central de ajuda não respondeu (issue #137, ADR-020).

O assistente grava em `perguntas_sem_resposta` cada pergunta que a Central de
ajuda não cobriu: só o texto já mascarado (CPF, CNPJ, e-mail, telefone e
números longos viram marcadores), a tela em que a pessoa estava e o instante,
sem usuário, conta nem empresa, por 90 dias. Este script só lê: lista as dos
últimos N dias agrupadas por tela, para o time escrever a ajuda que falta.

Uso, no diretório do backend:

    ./venv/bin/python scripts/perguntas_sem_resposta.py
    ./venv/bin/python scripts/perguntas_sem_resposta.py --dias 7

Banco: a mesma guarda de scripts/excluir_empresa.py. Por padrão, recusa
qualquer banco que não seja local; banco remoto só com `--banco-remoto` e o
host digitado como segunda confirmação.
"""

import argparse
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/perguntas_sem_resposta.py`, o sys.path recebe
# scripts/, não a raiz do repositório (mesmo caso de scripts/excluir_empresa.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import PerguntaSemResposta
from scripts.excluir_empresa import _banco_liberado

SEM_TELA = "(sem tela)"


def _criar_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": 10})


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lista as perguntas que a Central de ajuda não respondeu (issue #137)."
    )
    parser.add_argument(
        "--dias", type=int, default=30, help="quantos dias para trás (padrão 30, até 90)"
    )
    parser.add_argument(
        "--banco-remoto",
        action="store_true",
        help="aceita um DATABASE_URL que não seja local, depois de pedir o host digitado",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.dias <= 90:
        parser.error("--dias vai de 1 a 90 (as perguntas ficam 90 dias)")
    return args


def perguntas_por_tela(db: Session, dias: int) -> dict[str, list[PerguntaSemResposta]]:
    """As perguntas dos últimos `dias`, por tela, as mais recentes primeiro."""
    perguntas = db.scalars(
        select(PerguntaSemResposta)
        .where(PerguntaSemResposta.criado_em >= func.now() - timedelta(days=dias))
        .order_by(PerguntaSemResposta.criado_em.desc(), PerguntaSemResposta.id)
    )
    por_tela: dict[str, list[PerguntaSemResposta]] = defaultdict(list)
    for pergunta in perguntas:
        por_tela[pergunta.tela or SEM_TELA].append(pergunta)
    return dict(sorted(por_tela.items(), key=lambda item: (-len(item[1]), item[0])))


def main(argv: list[str] | None = None, perguntar: Callable[[str], str] = input) -> int:
    args = _argumentos(argv)
    url = settings.database_url
    if not _banco_liberado(url, args.banco_remoto, perguntar):
        return 1

    destino = make_url(url)
    engine = _criar_engine(url)
    try:
        with Session(engine) as db:
            por_tela = perguntas_por_tela(db, args.dias)
            print(f"Banco: {destino.host}/{destino.database}")
            total = sum(len(perguntas) for perguntas in por_tela.values())
            print(f"Perguntas sem resposta nos últimos {args.dias} dias: {total}")
            for tela, perguntas in por_tela.items():
                print()
                print(f"{tela} ({len(perguntas)})")
                for pergunta in perguntas:
                    print(f"  {pergunta.criado_em:%Y-%m-%d}  {pergunta.pergunta}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
