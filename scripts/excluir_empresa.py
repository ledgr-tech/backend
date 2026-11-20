"""Exclusão dos dados de uma conta, a pedido (issues #68 e #133, ADR-017).

O nome do arquivo ficou do tempo em que a empresa era o tenant. Desde a issue
#133 (ADR-019), o tenant é a CONTA, e este script exclui a conta inteira: os
usuários, os convites, todas as empresas clientes dela e os dados de cada uma.
Uma empresa cliente sozinha não passa por aqui: quem a remove é a conta, pelo
`DELETE /empresas/{id}` (issue #215), e os dados dela saem 30 dias depois por
scripts/apagar_empresas_removidas.py. O que sai de cada tabela é a lógica de
app/services/exclusao.py, a mesma dos dois scripts.

Não há rota para isto: a pessoa desativa a própria conta pelo `DELETE /me`,
e a exclusão dos dados é um processo interno, feito pelo time depois de
confirmar o pedido. O roteiro de quem atende está em
07-tecnico/processo-de-exclusao-de-dados.md, no vault.

Uso, no diretório do backend:

    ./venv/bin/python scripts/excluir_empresa.py --conta-id <uuid>
    ./venv/bin/python scripts/excluir_empresa.py --conta-id <uuid> --executar

`--empresa-id` continua aceito, como nome antigo de `--conta-id`: a migração
da #133 deu a cada conta o id da empresa de que veio.

Sem `--executar`, só simula: mostra a razão social e o CNPJ mascarado da
conta, quantas empresas clientes ela tem e quantas linhas sairiam de cada
tabela, sem apagar nada. Com `--executar`, pede quem está executando e o CNPJ
completo da conta, digitados, e apaga tudo numa transação só, na ordem de
`ETAPAS`. Na mesma transação grava uma linha em `registros_exclusao`, com o id
da conta, as contagens e o sha256 do CNPJ da conta, sem nada dos dados
apagados. Se qualquer passo falhar, nada é apagado.

Banco: o de DATABASE_URL, que em máquina de desenvolvimento aponta para o
Railway pelo `.env`. Por isso, por padrão, a guarda de
app/core/banco_local.py recusa qualquer banco que não seja local. Banco
remoto só com `--banco-remoto` e o host digitado como segunda confirmação.

Arquivos originais no R2 (issue #139, ADR-023): no plano avançado, o
arquivo de cada extrato fica cifrado no bucket próprio do R2, sob o prefixo da
conta. O script conta os objetos do prefixo na simulação e, depois das duas
confirmações, apaga todos ANTES da transação do banco. Para isso, quem roda
precisa das variáveis `ARQUIVO_ORIGINAL_R2_*` do token do bucket no ambiente
(não precisa da chave de cifra). Sem elas, e com algum extrato da conta com
`arquivo_chave`, a execução recusa antes de perguntar qualquer coisa: os
arquivos ficariam no R2 sem ninguém saber de quem são. Se o R2 falhar, nada é
apagado do banco; apagar por prefixo é idempotente, e rodar de novo resolve.

Nunca imprime nome nem e-mail de usuário: só contagens, a razão social e o
CNPJ mascarado. Do R2, só a contagem e o nome da classe de um erro.
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

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.config import settings
from app.core.plano import STATUS_EM_ANDAMENTO
from app.models import Conta, Empresa, Extrato, RegistroExclusao, Usuario
from app.services import exclusao as servico
from app.services.arquivo_original import (
    Armazenamento,
    armazenamento_do_ambiente,
    prefixo_da_conta,
)
from app.services.exclusao import ETAPAS, Escopo

# Tem `empresa_id`, mas fica: é o registro da própria exclusão.
TABELAS_QUE_FICAM = frozenset({RegistroExclusao.__tablename__})


# Em `registros_exclusao.contagens`, ao lado das tabelas: quantos objetos o
# script apagou no R2. Só entra quando o R2 estava configurado.
CHAVE_CONTAGEM_ARQUIVOS = "arquivos_originais_r2"
SEM_VARIAVEIS_DO_R2 = (
    "Sem as variáveis ARQUIVO_ORIGINAL_R2_* no ambiente, os arquivos originais ficariam "
    "no R2. Nada foi apagado."
)


MOTIVO_ASSINATURA_EM_ANDAMENTO = (
    "A conta tem assinatura em andamento na Stripe, e apagá-la não cancela a cobrança. "
    "Cancele a assinatura na Stripe e espere o webhook encerrá-la. Nada foi apagado."
)


def tabelas_apagadas() -> list[str]:
    return servico.tabelas_apagadas(ETAPAS)


def contar_linhas(db: Session, conta_id: uuid.UUID) -> dict[str, int]:
    """Quantas linhas de cada tabela são da conta, na ordem de `ETAPAS`."""
    return servico.contar_linhas(db, Escopo(conta_id), ETAPAS)


def _extratos_da_conta(conta_id: uuid.UUID):
    return Escopo(conta_id).extratos()


def hash_cnpj(cnpj: str) -> str:
    return hashlib.sha256(cnpj.encode()).hexdigest()


def cnpj_mascarado(cnpj: str) -> str:
    return f"{cnpj[:2]}.***.***/****-{cnpj[-2:]}"


@dataclass(frozen=True)
class Exclusao:
    registro_id: uuid.UUID
    # Na ordem de `ETAPAS`; o JSONB do registro não guarda a ordem das chaves.
    contagens: dict[str, int]


def excluir_conta(
    db: Session,
    conta_id: uuid.UUID,
    executado_por: str,
    arquivos_originais: int | None = None,
) -> Exclusao:
    """Apaga a conta, as empresas dela e tudo o que é delas, grava o registro e
    faz o commit, numa transação só. Qualquer erro desfaz tudo e sobe. A
    ordem, as travas e as condições estão em app/services/exclusao.py."""
    try:
        conta = db.get(Conta, conta_id)
        if conta is None:
            raise LookupError("conta não encontrada")
        cnpj = conta.cnpj
        contagens = servico.apagar_linhas(db, Escopo(conta_id), ETAPAS)
        if arquivos_originais is not None:
            contagens[CHAVE_CONTAGEM_ARQUIVOS] = arquivos_originais
        registro_id = uuid.uuid4()
        registro = RegistroExclusao(
            id=registro_id,
            empresa_id=conta_id,
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
            "Exclui uma conta, com todas as empresas clientes e os dados delas, a pedido "
            "(issues #68 e #133). Sem --executar, só simula."
        )
    )
    parser.add_argument(
        "--conta-id",
        "--empresa-id",
        dest="conta_id",
        required=True,
        type=uuid.UUID,
        help="id da conta (--empresa-id é o nome antigo)",
    )
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


def _armazenamento_do_ambiente() -> Armazenamento | None:
    return armazenamento_do_ambiente(settings)


def _extratos_com_original(db: Session, conta_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count()).where(
            Extrato.id.in_(_extratos_da_conta(conta_id)), Extrato.arquivo_chave.is_not(None)
        )
    )


def main(
    argv: list[str] | None = None,
    perguntar: Callable[[str], str] = input,
    criar_armazenamento: Callable[[], Armazenamento | None] = _armazenamento_do_ambiente,
) -> int:
    args = _argumentos(argv)
    url = settings.database_url
    if not _banco_liberado(url, args.banco_remoto, perguntar):
        return 1

    destino = make_url(url)
    engine = _criar_engine(url)
    try:
        with Session(engine) as db:
            conta = db.get(Conta, args.conta_id)
            if conta is None:
                print(f"Conta {args.conta_id} não encontrada em {destino.host}/{destino.database}.")
                return 1
            razao_social, cnpj = conta.razao_social, conta.cnpj
            assinatura = conta.assinatura_stripe
            if assinatura is not None and assinatura.status in STATUS_EM_ANDAMENTO:
                print(MOTIVO_ASSINATURA_EM_ANDAMENTO)
                return 1
            usuarios_ativos = db.scalar(
                select(func.count()).where(
                    Usuario.conta_id == conta.id, Usuario.removido_em.is_(None)
                )
            )
            contagens = contar_linhas(db, conta.id)
            com_original = _extratos_com_original(db, conta.id)
            # Fecha a leitura antes de esperar o que for digitado.
            db.rollback()

            prefixo = prefixo_da_conta(conta.id)
            objetos = None
            try:
                armazenamento = criar_armazenamento()
                if armazenamento is not None:
                    objetos = armazenamento.contar_prefixo(prefixo)
            except Exception as exc:  # noqa: BLE001 - a mensagem do boto3 traz o endpoint
                print(
                    f"Falha ao consultar os arquivos originais no R2 ({type(exc).__name__}). "
                    "Nada foi apagado."
                )
                return 1

            print(f"Banco:           {destino.host}/{destino.database}")
            print(f"Conta:           {args.conta_id}")
            print(f"Razão social:    {razao_social}")
            print(f"CNPJ:            {cnpj_mascarado(cnpj)}")
            print(f"Empresas:        {contagens[Empresa.__tablename__]}")
            print(f"Usuários ativos: {usuarios_ativos}")
            print("Linhas que seriam apagadas:" if not args.executar else "Linhas a apagar:")
            _imprimir_contagens(contagens)
            if objetos is not None:
                print(f"Arquivos originais no R2: {objetos}")
            elif com_original:
                print(
                    f"Arquivos originais no R2: {com_original} extrato(s) com original, mas as "
                    "variáveis ARQUIVO_ORIGINAL_R2_* não estão no ambiente."
                )

            if not args.executar:
                print("Simulação: nada foi apagado. Para apagar, rode de novo com --executar.")
                return 0
            if armazenamento is None and com_original:
                print(SEM_VARIAVEIS_DO_R2)
                return 1

            executado_por = perguntar("Quem está executando (fica no registro): ").strip()
            if not executado_por:
                print("Sem quem executou, não há registro. Nada foi apagado.")
                return 1
            # Com ou sem máscara, numérico ou alfanumérico (antes, tirar tudo
            # que não era dígito apagava as letras do CNPJ alfanumérico).
            digitado = normalizar_cnpj(perguntar("Digite o CNPJ completo da conta: "))
            if digitado != cnpj:
                print("O CNPJ digitado não confere. Nada foi apagado.")
                return 1

            # R2 antes do banco: se o R2 falhar, a conta continua inteira no
            # banco e o script pode rodar de novo.
            arquivos = None
            if armazenamento is not None:
                try:
                    arquivos = armazenamento.apagar_prefixo(prefixo)
                except Exception as exc:  # noqa: BLE001 - a mensagem do boto3 traz o endpoint
                    print(
                        f"Falha ao apagar os arquivos originais no R2 ({type(exc).__name__}). "
                        "Nada foi apagado do banco."
                    )
                    return 1

            exclusao = excluir_conta(db, args.conta_id, executado_por, arquivos_originais=arquivos)
            print("Conta excluída. Linhas apagadas:")
            _imprimir_contagens(exclusao.contagens)
            print(f"Registro da exclusão: {exclusao.registro_id}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
