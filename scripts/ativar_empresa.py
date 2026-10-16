"""Marca uma empresa como assinante ativa, ou desfaz (issue #122).

Até o gateway de pagamento existir, é assim que os pilotos ficam "ativo": a
situação da empresa (app/core/plano.py) passa a "ativo" com
`assinatura_ativa`, qualquer que seja o plano ou o teste. Com `--ativo false`,
a situação volta a sair do plano e do teste.

Uso, no diretório do backend:

    ./venv/bin/python scripts/ativar_empresa.py --empresa-id <uuid> --ativo true
    ./venv/bin/python scripts/ativar_empresa.py --empresa-id <uuid> --ativo false

Mostra a razão social, o CNPJ mascarado e a situação atual, pede o CNPJ
completo digitado como confirmação, grava e mostra a situação nova. A linha
da empresa é travada para gravar.

Banco: a mesma guarda de scripts/excluir_empresa.py. Por padrão, recusa
qualquer banco que não seja local; banco remoto só com `--banco-remoto` e o
host digitado como segunda confirmação.

Nunca imprime nome nem e-mail de usuário.
"""

import argparse
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/ativar_empresa.py`, o sys.path recebe scripts/,
# não a raiz do repositório (mesmo caso de scripts/excluir_empresa.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.config import settings
from app.core.plano import situacao_da_empresa
from app.models import Empresa
from scripts.excluir_empresa import _banco_liberado, cnpj_mascarado


def _criar_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": 10})


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Marca uma empresa como assinante ativa, ou desfaz (issue #122)."
    )
    parser.add_argument("--empresa-id", required=True, type=uuid.UUID, help="id da empresa")
    parser.add_argument(
        "--ativo",
        required=True,
        choices=["true", "false"],
        help="true marca a assinatura como ativa; false desfaz",
    )
    parser.add_argument(
        "--banco-remoto",
        action="store_true",
        help="aceita um DATABASE_URL que não seja local, depois de pedir o host digitado",
    )
    return parser.parse_args(argv)


def definir_assinatura(db: Session, empresa_id: uuid.UUID, ativa: bool) -> str:
    """Grava `assinatura_ativa` com a linha travada e devolve a situação nova."""
    try:
        empresa = db.scalar(
            select(Empresa)
            .where(Empresa.id == empresa_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if empresa is None:
            raise LookupError("empresa não encontrada")
        empresa.assinatura_ativa = ativa
        situacao = situacao_da_empresa(empresa)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return situacao


def main(argv: list[str] | None = None, perguntar: Callable[[str], str] = input) -> int:
    args = _argumentos(argv)
    ativa = args.ativo == "true"
    url = settings.database_url
    if not _banco_liberado(url, args.banco_remoto, perguntar):
        return 1

    destino = make_url(url)
    engine = _criar_engine(url)
    try:
        with Session(engine) as db:
            empresa = db.get(Empresa, args.empresa_id)
            if empresa is None:
                print(
                    f"Empresa {args.empresa_id} não encontrada em {destino.host}/{destino.database}."
                )
                return 1
            razao_social, cnpj = empresa.razao_social, empresa.cnpj
            situacao_atual = situacao_da_empresa(empresa)
            # Fecha a leitura antes de esperar o que for digitado.
            db.rollback()

            print(f"Banco:           {destino.host}/{destino.database}")
            print(f"Empresa:         {args.empresa_id}")
            print(f"Razão social:    {razao_social}")
            print(f"CNPJ:            {cnpj_mascarado(cnpj)}")
            print(f"Situação atual:  {situacao_atual}")
            print(f"Assinatura:      {'ativar' if ativa else 'desativar'}")

            digitado = normalizar_cnpj(perguntar("Digite o CNPJ completo da empresa: "))
            if digitado != cnpj:
                print("O CNPJ digitado não confere. Nada foi gravado.")
                return 1

            situacao_nova = definir_assinatura(db, args.empresa_id, ativa)
            print(f"Gravado. Situação nova: {situacao_nova}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
