"""Dá 14 dias de teste às contas existentes no lançamento da cobrança (ADR-026).

No diretório do backend:
    .venv/bin/python scripts/iniciar_teste_das_contas.py
    .venv/bin/python scripts/iniciar_teste_das_contas.py --aplicar

Dry-run por padrão: só conta, sem gravar. Elegíveis: sem plano, sem teste e
sem assinatura da Stripe (em qualquer status). Idempotente: nunca renova um
teste. Não imprime e-mail, CNPJ nem razão social. Banco remoto exige
--banco-remoto e confirmação do host, como scripts/ativar_empresa.py.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from app.core import plano
from app.core.config import settings
from app.models import Conta
from scripts.excluir_empresa import _banco_liberado


def iniciar_testes(db: Session, aplicar: bool = False) -> int:
    """Conta ou atualiza atomicamente só as elegíveis; devolve a quantidade."""
    elegiveis = (
        Conta.plano.is_(None),
        Conta.teste_termina_em.is_(None),
        ~Conta.assinatura_stripe.has(),
    )
    if not aplicar:
        return db.scalar(select(func.count()).select_from(Conta).where(*elegiveis))
    try:
        resultado = db.execute(
            update(Conta)
            .where(*elegiveis)
            .values(teste_termina_em=plano.agora() + plano.DURACAO_TESTE)
            .execution_options(synchronize_session=False)
        )
        quantidade = resultado.rowcount
        db.commit()
        return quantidade
    except BaseException:
        db.rollback()
        raise


def main(argv: list[str] | None = None, perguntar: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="grava os testes; padrão só conta")
    parser.add_argument(
        "--banco-remoto", action="store_true", help="aceita banco remoto com host digitado"
    )
    args = parser.parse_args(argv)
    url = settings.database_url
    if not _banco_liberado(url, args.banco_remoto, perguntar):
        return 1
    engine = create_engine(url, connect_args={"connect_timeout": 10})
    try:
        with Session(engine) as db:
            quantidade = iniciar_testes(db, aplicar=args.aplicar)
            print(f"{'Aplicado' if args.aplicar else 'Dry-run'}: {quantidade} conta(s).")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
