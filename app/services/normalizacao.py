"""Camada de normalização: extrato bruto -> lançamentos no schema comum,
executada como BackgroundTask depois do upload (issue #12).

Chamada por app/api/extratos.py via `background_tasks.add_task(normalizar_extrato, ...)`,
agendada depois que a resposta HTTP do upload já foi enviada com
status="pendente". `normalizar_extrato` é `def` normal, não `async def`, de
propósito: roda no threadpool do BackgroundTasks porque a Session do
SQLAlchemy é síncrona e o parsing com pandas é bloqueante — `async def`
aqui bloquearia o event loop pras outras requisições.

Fluxo: seta status="processando" (commita), parseia via
app.parsers.ofx/app.parsers.csv conforme o formato do extrato — que devolve
um `ResultadoParsing` (issue #13): lançamentos válidos + `ErroLinha` por
linha/transação que não normalizou, sem abortar o arquivo por causa delas.
Grava os lançamentos válidos via `INSERT ... ON CONFLICT (empresa_id,
extrato_id, hash_dedup) DO NOTHING` — dedupliação em reprocessamento
decidida na ADR-004, não checagem de duplicata na aplicação antes do
insert — e um `LinhaInvalida` por `ErroLinha`. O insert é em lote (issue
#51): o loop de parsing só acumula os valores numa lista e, depois dele,
grava em blocos de 1000 lançamentos por statement (`TAMANHO_BLOCO_INSERT`),
em vez de um round trip por linha. É a mesma técnica da issue #19 em
app/services/matching.py (`conciliar_extratos`, de 84 statements pra 1),
mas em blocos e não num lote só, porque um extrato pode ter volume bem maior
que uma conciliação. O cálculo de `ocorrencia`/`hash_dedup` não muda. Linhas idênticas dentro do
mesmo arquivo recebem `ocorrencia` 1, 2... que entra no hash (ADR-006), então
um pagamento duplicado de verdade não colapsa no ON CONFLICT. Status final:
- "concluido" se não houve nenhum erro de linha;
- "concluido_com_erros" se houve erro de linha mas pelo menos um lançamento
  válido;
- "erro" se houve erro de linha e zero lançamentos válidos (arquivo
  estruturalmente ok, mas nenhuma linha aproveitável) — as `linhas_invalidas`
  são persistidas de qualquer forma, é justamente o caso onde o relatório
  mais importa.

Erro estrutural do arquivo inteiro (OFXInvalidoError/CSVInvalidoError, ver
docstrings de app/parsers/ofx.py e app/parsers/csv.py) continua setando
status="erro" sem registrar `linhas_invalidas` — não há detalhe por linha
nesse caso, o arquivo inteiro nem chegou a ser parseado.

O try/except amplo no nível mais alto da função existe porque uma exceção
não tratada numa BackgroundTask do FastAPI é só logada pelo servidor e
nunca chega no cliente (a resposta HTTP já foi enviada) — sem ele, um erro
inesperado deixaria o extrato preso em "processando" pra sempre.
"""

import hashlib
import logging
import re
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import Extrato, Lancamento, LinhaInvalida
from app.parsers.csv import CSVInvalidoError, parse_csv
from app.parsers.ofx import OFXInvalidoError, parse_ofx
from app.parsers.tipos import ResultadoParsing

logger = logging.getLogger(__name__)

_ESPACOS_INTERNOS = re.compile(r"\s+")

# Lançamentos por statement de INSERT (issue #51). 1000 linhas x 9 colunas fica
# bem abaixo do limite de 32767 parâmetros por statement do protocolo do Postgres.
TAMANHO_BLOCO_INSERT = 1000


def _normalizar_descricao(descricao: str) -> str:
    """strip + casefold + espaços internos colapsados — sem stripar acento
    (acento faz parte do valor comparado)."""
    return _ESPACOS_INTERNOS.sub(" ", descricao.strip().casefold())


def _calcular_hash_dedup(
    valor: Decimal,
    data_lancamento: date,
    descricao: str,
    extrato_id: uuid.UUID,
    ocorrencia: int = 1,
) -> str:
    """sha256 hexdigest de valor+data+descrição normalizada+extrato_id (ADR-004),
    mais a ocorrência da linha idêntica no arquivo (ADR-006).

    Quando `ocorrencia` == 1 a fórmula é idêntica à original
    (`valor|data|descricao|extrato_id`), pra não invalidar os hashes de linhas
    já gravadas; só acrescenta `|{ocorrencia}` quando for maior que 1. Assim
    duas linhas idênticas no mesmo arquivo geram hashes distintos e não
    colapsam no ON CONFLICT, e reprocessar o mesmo arquivo segue idempotente.
    """
    bruto = f"{valor}|{data_lancamento.isoformat()}|{_normalizar_descricao(descricao)}|{extrato_id}"
    if ocorrencia > 1:
        bruto += f"|{ocorrencia}"
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _parse(formato: str, conteudo: bytes) -> ResultadoParsing:
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
            resultado = _parse(formato, conteudo)
        except (OFXInvalidoError, CSVInvalidoError) as exc:
            logger.warning(
                "normalizar_extrato: extrato %s com conteúdo inválido: %s", extrato_id, exc
            )
            db.rollback()
            _marcar_erro(db, extrato_id)
            return

        # Número de ordem de cada linha idêntica (valor|data|descrição
        # normalizada) dentro do arquivo — ADR-006.
        contagem_por_chave: Counter[tuple[Decimal, date, str]] = Counter()
        valores: list[dict] = []
        for lancamento in resultado.lancamentos:
            chave = (
                lancamento.valor,
                lancamento.data,
                _normalizar_descricao(lancamento.descricao),
            )
            contagem_por_chave[chave] += 1
            ocorrencia = contagem_por_chave[chave]
            hash_dedup = _calcular_hash_dedup(
                lancamento.valor, lancamento.data, lancamento.descricao, extrato_id, ocorrencia
            )
            valores.append(
                {
                    "id": uuid.uuid4(),
                    "empresa_id": extrato.empresa_id,
                    "extrato_id": extrato_id,
                    "data": lancamento.data,
                    "valor": lancamento.valor,
                    "descricao": lancamento.descricao,
                    "tipo": lancamento.tipo,
                    "hash_dedup": hash_dedup,
                    "ocorrencia": ocorrencia,
                }
            )

        # Insert em lote (issue #51): um statement por bloco de até
        # TAMANHO_BLOCO_INSERT linhas, não um por lançamento.
        for inicio in range(0, len(valores), TAMANHO_BLOCO_INSERT):
            bloco = valores[inicio : inicio + TAMANHO_BLOCO_INSERT]
            db.execute(
                pg_insert(Lancamento)
                .values(bloco)
                .on_conflict_do_nothing(index_elements=["empresa_id", "extrato_id", "hash_dedup"])
            )

        for erro in resultado.erros:
            db.add(
                LinhaInvalida(
                    extrato_id=extrato_id,
                    identificador=erro.identificador,
                    motivo=erro.motivo,
                )
            )

        if not resultado.erros:
            extrato.status = "concluido"
        elif resultado.lancamentos:
            extrato.status = "concluido_com_erros"
        else:
            # Arquivo estruturalmente ok, mas nenhuma linha aproveitável —
            # as linhas_invalidas acima são persistidas de qualquer forma.
            extrato.status = "erro"
        extrato.quantidade_lancamentos = len(resultado.lancamentos)
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
