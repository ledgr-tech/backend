"""Marca uma conta como assinante ativa, ou desfaz (issues #122 e #133).

O nome do arquivo ficou do tempo em que o plano era da empresa. Desde a issue
#133 (ADR-019), plano, teste e assinatura são da CONTA, e é ela que este
script ativa: todas as empresas clientes dela passam a valer como "ativo".

Até o gateway de pagamento existir, é assim que os pilotos ficam "ativo": a
situação da conta (app/core/plano.py) passa a "ativo" com
`assinatura_ativa`, qualquer que seja o plano ou o teste. Com `--ativo false`,
a situação volta a sair do plano e do teste.

Uso, no diretório do backend, com o id ou o CNPJ da conta (com ou sem
máscara):

    ./venv/bin/python scripts/ativar_empresa.py --conta <uuid ou CNPJ> --ativo true
    ./venv/bin/python scripts/ativar_empresa.py --conta <uuid ou CNPJ> --ativo false

`--empresa-id` continua aceito, como nome antigo de `--conta`: a migração da
#133 deu a cada conta o id da empresa de que veio.

Mostra a razão social, o CNPJ mascarado e a situação atual, pede o CNPJ
completo digitado como confirmação, grava e mostra a situação nova. A linha
da conta é travada para gravar.

Desde a issue #156 (ADR-025), o dia a dia é pelo painel `/admin` (POST
/admin/contas/{id}/piloto). O script continua para o banco local e para
quando o painel não estiver no ar, e grava o mesmo evento em `eventos_conta`
(`origem` "script", sem operador), com o `--motivo` dado. Sem mudança de
estado, não grava evento.

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
from app.core.plano import situacao_da_conta
from app.models import Conta, EventoConta
from scripts.excluir_empresa import _banco_liberado, cnpj_mascarado


def _criar_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": 10})


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Marca uma conta como assinante ativa, ou desfaz (issues #122 e #133)."
    )
    parser.add_argument(
        "--conta",
        "--empresa-id",
        dest="conta",
        required=True,
        help="id ou CNPJ da conta (--empresa-id é o nome antigo)",
    )
    parser.add_argument(
        "--ativo",
        required=True,
        choices=["true", "false"],
        help="true marca a assinatura como ativa; false desfaz",
    )
    parser.add_argument(
        "--motivo",
        default="Pelo scripts/ativar_empresa.py.",
        help="o motivo gravado no evento da conta (issue #156)",
    )
    parser.add_argument(
        "--banco-remoto",
        action="store_true",
        help="aceita um DATABASE_URL que não seja local, depois de pedir o host digitado",
    )
    return parser.parse_args(argv)


def buscar_conta(db: Session, conta: str) -> Conta | None:
    """A conta pelo id ou, se o texto não for um UUID, pelo CNPJ."""
    try:
        return db.get(Conta, uuid.UUID(conta.strip()))
    except ValueError:
        cnpj = normalizar_cnpj(conta)
        return db.scalar(select(Conta).where(Conta.cnpj == cnpj)) if cnpj else None


def definir_assinatura(
    db: Session, conta_id: uuid.UUID, ativa: bool, motivo: str = "Pelo scripts/ativar_empresa.py."
) -> str:
    """Grava `assinatura_ativa` com a linha travada, e o evento quando o estado
    muda (issue #156), e devolve a situação nova."""
    try:
        conta = db.scalar(
            select(Conta)
            .where(Conta.id == conta_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if conta is None:
            raise LookupError("conta não encontrada")
        if conta.assinatura_ativa != ativa:
            db.add(
                EventoConta(
                    conta_id=conta.id,
                    tipo="piloto_ligado" if ativa else "piloto_desligado",
                    motivo=motivo.strip()[:500] or "Pelo scripts/ativar_empresa.py.",
                    origem="script",
                )
            )
        conta.assinatura_ativa = ativa
        situacao = situacao_da_conta(conta)
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
            conta = buscar_conta(db, args.conta)
            if conta is None:
                print(f"Conta {args.conta} não encontrada em {destino.host}/{destino.database}.")
                return 1
            conta_id, razao_social, cnpj = conta.id, conta.razao_social, conta.cnpj
            situacao_atual = situacao_da_conta(conta)
            # Fecha a leitura antes de esperar o que for digitado.
            db.rollback()

            print(f"Banco:           {destino.host}/{destino.database}")
            print(f"Conta:           {conta_id}")
            print(f"Razão social:    {razao_social}")
            print(f"CNPJ:            {cnpj_mascarado(cnpj)}")
            print(f"Situação atual:  {situacao_atual}")
            print(f"Assinatura:      {'ativar' if ativa else 'desativar'}")

            digitado = normalizar_cnpj(perguntar("Digite o CNPJ completo da conta: "))
            if digitado != cnpj:
                print("O CNPJ digitado não confere. Nada foi gravado.")
                return 1

            situacao_nova = definir_assinatura(db, conta_id, ativa, args.motivo)
            print(f"Gravado. Situação nova: {situacao_nova}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
