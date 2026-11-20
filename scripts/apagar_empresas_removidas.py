"""Apaga de vez as empresas removidas há mais de 30 dias (issue #215, ADR-019).

`DELETE /empresas/{id}` só desativa a empresa e marca `apagar_em` (30 dias
depois). Este script, que roda todo dia como cron, apaga as que passaram desse
dia: as linhas de todas as tabelas dela e os arquivos originais no R2. A lógica
do que sai é a mesma da exclusão da conta (app/services/exclusao.py), com o
escopo de uma empresa; scripts/excluir_empresa.py é quem apaga a conta inteira.

Uso, no diretório do backend (na Railway, como serviço de cron diário, com o
mesmo `DATABASE_URL` e as variáveis `ARQUIVO_ORIGINAL_R2_*` do serviço da API):

    python scripts/apagar_empresas_removidas.py
    python scripts/apagar_empresas_removidas.py --simular

`--simular` só lista as vencidas e as contagens, sem apagar nada.

Idempotente: o que já foi apagado não aparece de novo, e rodar duas vezes
seguidas não faz nada na segunda. Uma empresa por transação: a falha de uma
(o R2 fora do ar, por exemplo) desfaz só ela, e as outras seguem. A linha da
empresa é travada antes de tudo, a mesma trava do `restaurar`: se alguém
restaurou depois da listagem, a empresa é pulada, e o R2 só é apagado depois
da trava, para uma restauração nunca ficar sem os arquivos. Se o R2 falhar,
nada é apagado do banco. Sem as variáveis do R2 no ambiente e com algum extrato
da empresa com `arquivo_chave`, a empresa é pulada, com erro, em vez de deixar
arquivos soltos no bucket.

Cada exclusão grava uma linha em `registros_exclusao` (id da empresa, hash do
CNPJ e contagens, nada dos dados), na mesma transação.

Log: só ids e contagens, nunca razão social, CNPJ nem nome de pessoa. Sai com
código 1 se alguma empresa falhou, para o cron da Railway acusar.
"""

import argparse
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Rodado como `python scripts/apagar_empresas_removidas.py`, o sys.path recebe
# scripts/, não a raiz do repositório (mesmo caso de scripts/excluir_empresa.py).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Extrato
from app.services import exclusao as servico
from app.services.arquivo_original import (
    Armazenamento,
    armazenamento_do_ambiente,
    prefixo_da_empresa,
)

logger = logging.getLogger("apagar_empresas_removidas")

EXECUTADO_POR = "apagar_empresas_removidas"


def _agora() -> datetime:
    return datetime.now(UTC)


def _armazenamento_do_ambiente() -> Armazenamento | None:
    return armazenamento_do_ambiente(settings)


def _extratos_com_original(db: Session, escopo: servico.Escopo) -> int:
    return db.scalar(
        select(func.count())
        .where(Extrato.id.in_(escopo.extratos()), Extrato.arquivo_chave.is_not(None))
        .execution_options(**servico.COM_REMOVIDAS)
    )


def apagar_uma(
    db: Session,
    empresa_id: uuid.UUID,
    agora: datetime,
    armazenamento: Armazenamento | None,
) -> servico.Exclusao | None:
    """Apaga uma empresa vencida numa transação. Devolve `None` se ela já não
    está vencida (restaurada ou apagada desde a listagem). Qualquer erro desfaz
    a transação e sobe."""
    try:
        empresa = servico.travar_se_vencida(db, empresa_id, agora)
        if empresa is None:
            db.rollback()
            return None
        escopo = servico.Escopo(empresa.conta_id, empresa.id)
        arquivos = None
        if armazenamento is not None:
            arquivos = armazenamento.apagar_prefixo(
                prefixo_da_empresa(empresa.conta_id, empresa.id)
            )
        elif _extratos_com_original(db, escopo):
            raise RuntimeError("sem as variáveis ARQUIVO_ORIGINAL_R2_* e com arquivo original")
        resultado = servico.apagar_empresa(db, empresa, EXECUTADO_POR, arquivos_originais=arquivos)
        db.commit()
        return resultado
    except BaseException:
        db.rollback()
        raise


def main(
    argv: list[str] | None = None,
    agora: Callable[[], datetime] = _agora,
    criar_armazenamento: Callable[[], Armazenamento | None] = _armazenamento_do_ambiente,
    criar_sessao: Callable[[], Session] = SessionLocal,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apaga de vez as empresas removidas cujo prazo de 30 dias para restaurar "
            "acabou (issue #215)."
        )
    )
    parser.add_argument(
        "--simular", action="store_true", help="só lista as vencidas e as contagens"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    momento = agora()
    with criar_sessao() as db:
        vencidas = servico.empresas_vencidas(db, momento)
        db.rollback()
    logger.info("apagar_empresas_removidas vencidas=%d simular=%s", len(vencidas), args.simular)
    if not vencidas:
        return 0

    if args.simular:
        with criar_sessao() as db:
            for conta_id, empresa_id in vencidas:
                contagens = servico.contar_linhas(db, servico.Escopo(conta_id, empresa_id))
                logger.info(
                    "apagar_empresas_removidas empresa_id=%s conta_id=%s linhas=%d (simulação)",
                    empresa_id,
                    conta_id,
                    sum(contagens.values()),
                )
            db.rollback()
        return 0

    try:
        armazenamento = criar_armazenamento()
    except Exception as exc:  # noqa: BLE001 - a mensagem do boto3 traz o endpoint
        logger.error(
            "apagar_empresas_removidas resultado=erro_r2 classe=%s apagadas=0", type(exc).__name__
        )
        return 1

    apagadas = puladas = falhas = 0
    for conta_id, empresa_id in vencidas:
        try:
            with criar_sessao() as db:
                resultado = apagar_uma(db, empresa_id, momento, armazenamento)
        except Exception as exc:  # noqa: BLE001 - uma empresa não derruba as outras
            falhas += 1
            logger.error(
                "apagar_empresas_removidas empresa_id=%s conta_id=%s resultado=falha classe=%s",
                empresa_id,
                conta_id,
                type(exc).__name__,
            )
            continue
        if resultado is None:
            puladas += 1
            logger.info(
                "apagar_empresas_removidas empresa_id=%s resultado=pulada (restaurada ou apagada)",
                empresa_id,
            )
            continue
        apagadas += 1
        logger.info(
            "apagar_empresas_removidas empresa_id=%s conta_id=%s resultado=apagada linhas=%d "
            "arquivos_originais=%s registro_id=%s",
            empresa_id,
            conta_id,
            sum(v for k, v in resultado.contagens.items() if k != servico.CHAVE_CONTAGEM_ARQUIVOS),
            resultado.contagens.get(servico.CHAVE_CONTAGEM_ARQUIVOS, "-"),
            resultado.registro_id,
        )
    logger.info(
        "apagar_empresas_removidas apagadas=%d puladas=%d falhas=%d", apagadas, puladas, falhas
    )
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
