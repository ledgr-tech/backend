"""Camada de normalização: extrato bruto -> lançamentos no schema comum,
executada como BackgroundTask depois do upload (issue #12).

Chamada por app/api/extratos.py via `background_tasks.add_task(normalizar_extrato, ...)`,
agendada depois que a resposta HTTP do upload já foi enviada com
status="pendente". `normalizar_extrato` é `def` normal, não `async def`, de
propósito: roda no threadpool do BackgroundTasks porque a Session do
SQLAlchemy é síncrona e o parsing com pandas é bloqueante — `async def`
aqui bloquearia o event loop pras outras requisições.

Fluxo: seta status="processando" (commita), parseia via
app.parsers.ofx/app.parsers.csv conforme o formato do extrato, grava os
lançamentos via `INSERT ... ON CONFLICT (empresa_id, extrato_id,
hash_dedup) DO NOTHING` — dedupliação em reprocessamento decidida na
ADR-004, não checagem de duplicata na aplicação antes do insert — e seta
status="concluido"/quantidade_lancamentos. Em erro de parsing
(OFXInvalidoError/CSVInvalidoError), seta status="erro". O try/except
amplo no nível mais alto da função existe porque uma exceção não tratada
numa BackgroundTask do FastAPI é só logada pelo servidor e nunca chega no
cliente (a resposta HTTP já foi enviada) — sem ele, um erro inesperado
deixaria o extrato preso em "processando" pra sempre.

Fora de escopo aqui (issue #13, não antecipado): arquivo com linha
quebrada falha o extrato inteiro, porque os parsers das #8/#9 abortam no
primeiro erro de linha. Tolerância por linha com relatório de motivo é
escopo da #13 — os parsers não são alterados por esta issue.
"""

import hashlib
import logging
import re
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import Extrato, Lancamento
from app.parsers.csv import CSVInvalidoError, parse_csv
from app.parsers.ofx import OFXInvalidoError, parse_ofx
from app.parsers.tipos import LancamentoNormalizado

logger = logging.getLogger(__name__)

_ESPACOS_INTERNOS = re.compile(r"\s+")


def _calcular_hash_dedup(
    valor: Decimal, data_lancamento: date, descricao: str, extrato_id: uuid.UUID
) -> str:
    """sha256 hexdigest de valor+data+descrição normalizada+extrato_id (ADR-004).

    Descrição normalizada: strip + casefold + espaços internos colapsados —
    sem stripar acento (acento faz parte do valor comparado).
    """
    descricao_normalizada = _ESPACOS_INTERNOS.sub(" ", descricao.strip().casefold())
    bruto = f"{valor}|{data_lancamento.isoformat()}|{descricao_normalizada}|{extrato_id}"
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _parse(formato: str, conteudo: bytes) -> list[LancamentoNormalizado]:
    if formato == "ofx":
        return parse_ofx(conteudo)
    if formato == "csv":
        return parse_csv(conteudo)
    raise ValueError(f"Formato de extrato não suportado: {formato!r}")


def _marcar_erro(db: Session, extrato_id: uuid.UUID) -> None:
    """Recarrega o extrato (o objeto em memória pode estar expirado depois de
    um rollback/commit anterior) e seta status="erro", commitando."""
    extrato = db.get(Extrato, extrato_id)
    if extrato is not None:
        extrato.status = "erro"
        db.commit()


def normalizar_extrato(extrato_id: uuid.UUID, formato: str, conteudo: bytes) -> None:
    """Parseia `conteudo` e grava os lançamentos normalizados do extrato `extrato_id`.

    Roda como BackgroundTask depois do upload — abre a própria sessão (a do
    request já fechou quando esta função executa, não reaproveita a `db`
    do endpoint).
    """
    db = SessionLocal()
    try:
        extrato = db.get(Extrato, extrato_id)
        if extrato is None:
            logger.error("normalizar_extrato: extrato %s não encontrado", extrato_id)
            return

        extrato.status = "processando"
        db.commit()

        try:
            lancamentos = _parse(formato, conteudo)
        except (OFXInvalidoError, CSVInvalidoError) as exc:
            logger.warning(
                "normalizar_extrato: extrato %s com conteúdo inválido: %s", extrato_id, exc
            )
            db.rollback()
            _marcar_erro(db, extrato_id)
            return

        for lancamento in lancamentos:
            hash_dedup = _calcular_hash_dedup(
                lancamento.valor, lancamento.data, lancamento.descricao, extrato_id
            )
            stmt = (
                pg_insert(Lancamento)
                .values(
                    id=uuid.uuid4(),
                    empresa_id=extrato.empresa_id,
                    extrato_id=extrato_id,
                    data=lancamento.data,
                    valor=lancamento.valor,
                    descricao=lancamento.descricao,
                    tipo=lancamento.tipo,
                    hash_dedup=hash_dedup,
                )
                .on_conflict_do_nothing(index_elements=["empresa_id", "extrato_id", "hash_dedup"])
            )
            db.execute(stmt)

        extrato.status = "concluido"
        extrato.quantidade_lancamentos = len(lancamentos)
        db.commit()
    except Exception:
        # Amplo de propósito — ver docstring do módulo: garante que o
        # extrato nunca fica preso em "processando" por um erro que os
        # except específicos acima não previram (ex: falha de conexão no
        # meio do insert).
        logger.exception("normalizar_extrato: falha inesperada processando extrato %s", extrato_id)
        db.rollback()
        try:
            _marcar_erro(db, extrato_id)
        except Exception:
            logger.exception("normalizar_extrato: falha ao marcar extrato %s como erro", extrato_id)
            db.rollback()
    finally:
        db.close()
