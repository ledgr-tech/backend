"""Exclusão dos dados de uma conta, a pedido (issues #68 e #133, ADR-017).

O nome do arquivo ficou do tempo em que a empresa era o tenant. Desde a issue
#133 (ADR-019), o tenant é a CONTA, e este script exclui a conta inteira: os
usuários, os convites, todas as empresas clientes dela e os dados de cada uma.
Excluir uma empresa cliente sozinha fica para a issue #138.

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

from sqlalchemy import ColumnElement, Engine, create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.banco_local import BancoNaoLocal, exigir_banco_local
from app.core.cnpj import normalizar as normalizar_cnpj
from app.core.config import settings
from app.models import (
    Base,
    ChaveApi,
    CodigoOAuth,
    Conciliacao,
    ConexaoMcp,
    Configuracao,
    Conta,
    Convite,
    DecisaoLinha,
    Empresa,
    EmpresaFavorita,
    EventoConta,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    LinkRelatorio,
    OperadorLedgr,
    PedidoAutorizacaoOAuth,
    RegistroExclusao,
    TokenEmail,
    TokenOAuth,
    UsoAssistente,
    Usuario,
)
from app.services.arquivo_original import (
    Armazenamento,
    armazenamento_do_ambiente,
    prefixo_da_conta,
)


def _empresas_da_conta(conta_id: uuid.UUID):
    return select(Empresa.id).where(Empresa.conta_id == conta_id)


def _extratos_da_conta(conta_id: uuid.UUID):
    return select(Extrato.id).where(Extrato.empresa_id.in_(_empresas_da_conta(conta_id)))


def _usuarios_da_conta(conta_id: uuid.UUID):
    return select(Usuario.id).where(Usuario.conta_id == conta_id)


def _conexoes_da_conta(conta_id: uuid.UUID):
    return select(ConexaoMcp.id).where(ConexaoMcp.conta_id == conta_id)


def _da_conta(coluna_empresa_id):
    """Condição das tabelas por `empresa_id`: as linhas de todas as empresas da conta."""
    return lambda c: coluna_empresa_id.in_(_empresas_da_conta(c))


# Ordem das FKs, conferida nos models: quem aponta para uma tabela sai antes
# dela. As tabelas por `empresa_id` saem pelas empresas da conta;
# `linhas_invalidas` e `tokens_email` saem pelo extrato e pelo usuário;
# `tokens_oauth` e `codigos_oauth`, pela conexão MCP (issue #136);
# `empresas_favoritas`, pelo `conta_id` dela (issue #157), antes de usuários e
# empresas; `links_relatorio` (issue #165), antes dos fechamentos;
# `eventos_conta` e `operadores_ledgr` (issue #156), antes dos usuários. O
# `acessos_admin` fica: não tem FK nem `conta_id`, é o registro de quem do
# Ledgr consultou a conta. Os `clientes_oauth` ficam: não são de nenhuma conta.
# tests/test_excluir_empresa.py confere a ordem e que nenhuma tabela ligada à
# conta ou às empresas dela ficou de fora.
ETAPAS: tuple[tuple[type[Base], Callable[[uuid.UUID], ColumnElement[bool]]], ...] = (
    (ExplicacaoDivergencia, _da_conta(ExplicacaoDivergencia.empresa_id)),
    (DecisaoLinha, _da_conta(DecisaoLinha.empresa_id)),
    (Conciliacao, _da_conta(Conciliacao.empresa_id)),
    (ExecucaoConciliacao, _da_conta(ExecucaoConciliacao.empresa_id)),
    (LinkRelatorio, _da_conta(LinkRelatorio.empresa_id)),
    (Fechamento, _da_conta(Fechamento.empresa_id)),
    (LinhaInvalida, lambda c: LinhaInvalida.extrato_id.in_(_extratos_da_conta(c))),
    (Lancamento, _da_conta(Lancamento.empresa_id)),
    (Extrato, _da_conta(Extrato.empresa_id)),
    (Convite, lambda c: Convite.conta_id == c),
    (ChaveApi, lambda c: ChaveApi.conta_id == c),
    (TokenOAuth, lambda c: TokenOAuth.conexao_id.in_(_conexoes_da_conta(c))),
    (CodigoOAuth, lambda c: CodigoOAuth.conexao_id.in_(_conexoes_da_conta(c))),
    (ConexaoMcp, lambda c: ConexaoMcp.conta_id == c),
    (PedidoAutorizacaoOAuth, lambda c: PedidoAutorizacaoOAuth.conta_id == c),
    (TokenEmail, lambda c: TokenEmail.usuario_id.in_(_usuarios_da_conta(c))),
    (Configuracao, _da_conta(Configuracao.empresa_id)),
    (EmpresaFavorita, lambda c: EmpresaFavorita.conta_id == c),
    (EventoConta, lambda c: EventoConta.conta_id == c),
    (OperadorLedgr, lambda c: OperadorLedgr.usuario_id.in_(_usuarios_da_conta(c))),
    (Usuario, lambda c: Usuario.conta_id == c),
    (Empresa, lambda c: Empresa.conta_id == c),
    (UsoAssistente, lambda c: UsoAssistente.conta_id == c),
    (Conta, lambda c: Conta.id == c),
)

# Tem `empresa_id`, mas fica: é o registro da própria exclusão.
TABELAS_QUE_FICAM = frozenset({RegistroExclusao.__tablename__})


# Em `registros_exclusao.contagens`, ao lado das tabelas: quantos objetos o
# script apagou no R2. Só entra quando o R2 estava configurado.
CHAVE_CONTAGEM_ARQUIVOS = "arquivos_originais_r2"
SEM_VARIAVEIS_DO_R2 = (
    "Sem as variáveis ARQUIVO_ORIGINAL_R2_* no ambiente, os arquivos originais ficariam "
    "no R2. Nada foi apagado."
)


def tabelas_apagadas() -> list[str]:
    return [model.__tablename__ for model, _ in ETAPAS]


def contar_linhas(db: Session, conta_id: uuid.UUID) -> dict[str, int]:
    """Quantas linhas de cada tabela são da conta, na ordem de `ETAPAS`."""
    return {
        model.__tablename__: db.scalar(
            select(func.count()).select_from(model).where(cond(conta_id))
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


def excluir_conta(
    db: Session,
    conta_id: uuid.UUID,
    executado_por: str,
    arquivos_originais: int | None = None,
) -> Exclusao:
    """Apaga a conta, as empresas dela e tudo o que é delas, grava o registro e
    faz o commit, numa transação só. Qualquer erro desfaz tudo e sobe.

    A linha da conta e as das empresas dela são travadas antes de tudo: as
    tabelas com `empresa_id` têm FK para a empresa, e usuários, convites e
    empresas, para a conta, então um upload ou cadastro simultâneo espera e
    depois falha pela FK, em vez de deixar linha órfã."""
    try:
        conta = db.scalar(select(Conta).where(Conta.id == conta_id).with_for_update())
        if conta is None:
            raise LookupError("conta não encontrada")
        db.execute(select(Empresa.id).where(Empresa.conta_id == conta_id).with_for_update())
        cnpj = conta.cnpj
        contagens = {}
        for model, cond in ETAPAS:
            resultado = db.execute(
                delete(model).where(cond(conta_id)),
                execution_options={"synchronize_session": False},
            )
            contagens[model.__tablename__] = resultado.rowcount
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
