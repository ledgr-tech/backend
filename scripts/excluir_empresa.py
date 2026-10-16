"""Exclusão dos dados de uma empresa, a pedido (issue #68, ADR-017).

Não há rota para isto: a pessoa desativa a própria conta pelo `DELETE /me`,
e a exclusão da empresa é um processo interno, feito pelo time depois de
confirmar o pedido. O roteiro de quem atende está em
07-tecnico/processo-de-exclusao-de-dados.md, no vault.

Uso, no diretório do backend:

    ./venv/bin/python scripts/excluir_empresa.py --empresa-id <uuid>
    ./venv/bin/python scripts/excluir_empresa.py --empresa-id <uuid> --executar

Sem `--executar`, só simula: mostra a razão social, o CNPJ mascarado e
quantas linhas sairiam de cada tabela, sem apagar nada. Com `--executar`,
pede quem está executando e o CNPJ completo da empresa, digitados, e apaga
tudo numa transação só, na ordem de `ETAPAS`. Na mesma transação grava uma
linha em `registros_exclusao`, com as contagens e o sha256 do CNPJ, sem nada
dos dados apagados. Se qualquer passo falhar, nada é apagado.

Banco: o de DATABASE_URL, que em máquina de desenvolvimento aponta para o
Railway pelo `.env`. Por isso, por padrão, a guarda de
app/core/banco_local.py recusa qualquer banco que não seja local. Banco
remoto só com `--banco-remoto` e o host digitado como segunda confirmação.

Nunca imprime nome nem e-mail de usuário: só contagens, a razão social e o
CNPJ mascarado.
"""

import argparse
import hashlib
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/excluir_empresa.py`, o sys.path recebe scripts/,
# não a raiz do repositório (mesmo caso de scripts/ensaio_restore.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import ColumnElement, Engine, create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.config import settings
from app.models import (
    Base,
    Conciliacao,
    Configuracao,
    Convite,
    DecisaoLinha,
    Empresa,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    RegistroExclusao,
    TokenEmail,
    Usuario,
)


def _extratos_da_empresa(empresa_id: uuid.UUID):
    return select(Extrato.id).where(Extrato.empresa_id == empresa_id)


def _usuarios_da_empresa(empresa_id: uuid.UUID):
    return select(Usuario.id).where(Usuario.empresa_id == empresa_id)


# Ordem das FKs, conferida nos models: quem aponta para uma tabela sai antes
# dela. `linhas_invalidas` e `tokens_email` não têm `empresa_id` e saem pelo
# extrato e pelo usuário. tests/test_excluir_empresa.py confere a ordem e que
# nenhuma tabela ligada à empresa ficou de fora.
ETAPAS: tuple[tuple[type[Base], Callable[[uuid.UUID], ColumnElement[bool]]], ...] = (
    (ExplicacaoDivergencia, lambda e: ExplicacaoDivergencia.empresa_id == e),
    (DecisaoLinha, lambda e: DecisaoLinha.empresa_id == e),
    (Conciliacao, lambda e: Conciliacao.empresa_id == e),
    (ExecucaoConciliacao, lambda e: ExecucaoConciliacao.empresa_id == e),
    (Fechamento, lambda e: Fechamento.empresa_id == e),
    (LinhaInvalida, lambda e: LinhaInvalida.extrato_id.in_(_extratos_da_empresa(e))),
    (Lancamento, lambda e: Lancamento.empresa_id == e),
    (Extrato, lambda e: Extrato.empresa_id == e),
    (Convite, lambda e: Convite.empresa_id == e),
    (TokenEmail, lambda e: TokenEmail.usuario_id.in_(_usuarios_da_empresa(e))),
    (Configuracao, lambda e: Configuracao.empresa_id == e),
    (Usuario, lambda e: Usuario.empresa_id == e),
    (Empresa, lambda e: Empresa.id == e),
)

# Tem `empresa_id`, mas fica: é o registro da própria exclusão.
TABELAS_QUE_FICAM = frozenset({RegistroExclusao.__tablename__})


def tabelas_apagadas() -> list[str]:
    return [model.__tablename__ for model, _ in ETAPAS]


def contar_linhas(db: Session, empresa_id: uuid.UUID) -> dict[str, int]:
    """Quantas linhas de cada tabela são da empresa, na ordem de `ETAPAS`."""
    return {
        model.__tablename__: db.scalar(
            select(func.count()).select_from(model).where(cond(empresa_id))
        )
        for model, cond in ETAPAS
    }


def hash_cnpj(cnpj: str) -> str:
    return hashlib.sha256(cnpj.encode()).hexdigest()


def cnpj_mascarado(cnpj: str) -> str:
    return f"{cnpj[:2]}.***.***/****-{cnpj[-2:]}"


@dataclass(frozen=True)
class Exclusao:
    registro_id: uuid.UUID
    # Na ordem de `ETAPAS`; o JSONB do registro não guarda a ordem das chaves.
    contagens: dict[str, int]


def excluir_empresa(db: Session, empresa_id: uuid.UUID, executado_por: str) -> Exclusao:
    """Apaga a empresa e tudo o que é dela, grava o registro e faz o commit,
    numa transação só. Qualquer erro desfaz tudo e sobe.

    A linha da empresa é travada antes de tudo: as tabelas com `empresa_id`
    têm FK para ela, então um upload ou cadastro simultâneo espera e depois
    falha pela FK, em vez de deixar linha órfã."""
    try:
        empresa = db.scalar(select(Empresa).where(Empresa.id == empresa_id).with_for_update())
        if empresa is None:
            raise LookupError("empresa não encontrada")
        cnpj = empresa.cnpj
        contagens = {}
        for model, cond in ETAPAS:
            resultado = db.execute(
                delete(model).where(cond(empresa_id)),
                execution_options={"synchronize_session": False},
            )
            contagens[model.__tablename__] = resultado.rowcount
        registro_id = uuid.uuid4()
        registro = RegistroExclusao(
            id=registro_id,
            empresa_id=empresa_id,
            cnpj_sha256=hash_cnpj(cnpj),
            contagens=contagens,
            executado_por=executado_por,
        )
        db.add(registro)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return Exclusao(registro_id=registro_id, contagens=contagens)


def _criar_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": 10})


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exclui os dados de uma empresa, a pedido (issue #68). Sem --executar, " "só simula."
        )
    )
    parser.add_argument("--empresa-id", required=True, type=uuid.UUID, help="id da empresa")
    parser.add_argument(
        "--executar",
        action="store_true",
        help="apaga de verdade, depois de pedir o CNPJ completo digitado",
    )
    parser.add_argument(
        "--banco-remoto",
        action="store_true",
        help="aceita um DATABASE_URL que não seja local, depois de pedir o host digitado",
    )
    return parser.parse_args(argv)


def _banco_liberado(url: str, banco_remoto: bool, perguntar: Callable[[str], str]) -> bool:
    try:
        exigir_banco_local(url)
        return True
    except BancoNaoLocal as erro:
        if not banco_remoto:
            print(f"Recusado: {erro}")
            print("Para um banco remoto, rode de novo com --banco-remoto.")
            return False
    host = make_url(url).host or ""
    print(f"ATENÇÃO: DATABASE_URL aponta para um banco remoto, no host {host}.")
    if not host or perguntar("Para continuar, digite o host do banco: ").strip() != host:
        print("O host digitado não confere. Nada foi feito.")
        return False
    return True


def _imprimir_contagens(contagens: dict[str, int]) -> None:
    largura = max(len(tabela) for tabela in contagens)
    for tabela, quantidade in contagens.items():
        print(f"  {tabela:<{largura}}  {quantidade}")
    print(f"  {'total':<{largura}}  {sum(contagens.values())}")


def main(argv: list[str] | None = None, perguntar: Callable[[str], str] = input) -> int:
    args = _argumentos(argv)
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
            usuarios_ativos = db.scalar(
                select(func.count()).where(
                    Usuario.empresa_id == empresa.id, Usuario.removido_em.is_(None)
                )
            )
            contagens = contar_linhas(db, empresa.id)
            # Fecha a leitura antes de esperar o que for digitado.
            db.rollback()

            print(f"Banco:           {destino.host}/{destino.database}")
            print(f"Empresa:         {args.empresa_id}")
            print(f"Razão social:    {razao_social}")
            print(f"CNPJ:            {cnpj_mascarado(cnpj)}")
            print(f"Usuários ativos: {usuarios_ativos}")
            print("Linhas que seriam apagadas:" if not args.executar else "Linhas a apagar:")
            _imprimir_contagens(contagens)

            if not args.executar:
                print("Simulação: nada foi apagado. Para apagar, rode de novo com --executar.")
                return 0

            executado_por = perguntar("Quem está executando (fica no registro): ").strip()
            if not executado_por:
                print("Sem quem executou, não há registro. Nada foi apagado.")
                return 1
            # Com ou sem máscara, numérico ou alfanumérico (antes, tirar tudo
            # que não era dígito apagava as letras do CNPJ alfanumérico).
            digitado = normalizar_cnpj(perguntar("Digite o CNPJ completo da empresa: "))
            if digitado != cnpj:
                print("O CNPJ digitado não confere. Nada foi apagado.")
                return 1

            exclusao = excluir_empresa(db, args.empresa_id, executado_por)
            print("Empresa excluída. Linhas apagadas:")
            _imprimir_contagens(exclusao.contagens)
            print(f"Registro da exclusão: {exclusao.registro_id}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
