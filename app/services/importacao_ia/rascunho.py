"""Rascunho da importação via IA no banco (issue #158, ADR-013).

O rascunho é um `Extrato` com `origem` "sistema", `formato` "json" e `status`
"rascunho" (`STATUS_RASCUNHO`), com os lançamentos gravados como os da
normalização (app/services/normalizacao.py): o mesmo `tipo` pelo sinal e o
mesmo `hash_dedup` com `ocorrencia`, mais a `posicao` no JSON. A confirmação
(#159) renumera `ocorrencia` e refaz o hash pela `posicao`, porque as
correções do rascunho mudam valor, data e descrição.

Expira em `VALIDADE` desde o `criado_em`. Vencido, é tratado como
inexistente (`rascunho_da_empresa` devolve `None`) e apagado aos poucos por
`apagar_vencidos`, chamada a cada importação nova, no máximo
`LIMPEZA_POR_IMPORTACAO` por vez, os mais antigos primeiro. Nenhum cron.

Descartar (`descartar`) apaga os lançamentos e o extrato. Nenhuma outra
tabela aponta para um rascunho: ele nunca entra em conciliação, nunca tem
`linhas_invalidas` e não guarda o arquivo no R2 (o JSON não é o original,
ADR-013).
"""

import uuid
from datetime import timedelta

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.models import Extrato, Lancamento
from app.models.extrato import STATUS_RASCUNHO
from app.services.importacao_ia.leitura import LancamentoImportado
from app.services.normalizacao import (
    TAMANHO_BLOCO_INSERT,
    _calcular_hash_dedup,
    _normalizar_descricao,
)

VALIDADE = timedelta(days=7)
LIMPEZA_POR_IMPORTACAO = 100


def _vigente():
    """O rascunho ainda vale: não passou de `VALIDADE`."""
    return Extrato.criado_em >= func.now() - VALIDADE


def apagar_vencidos(db: Session) -> int:
    """Apaga até `LIMPEZA_POR_IMPORTACAO` rascunhos vencidos, de qualquer
    conta, os mais antigos primeiro, sem commit. `SKIP LOCKED` para não esperar
    um descarte ou uma confirmação em andamento."""
    vencidos = db.scalars(
        select(Extrato.id)
        .where(Extrato.status == STATUS_RASCUNHO, Extrato.criado_em < func.now() - VALIDADE)
        .order_by(Extrato.criado_em, Extrato.id)
        .limit(LIMPEZA_POR_IMPORTACAO)
        .with_for_update(skip_locked=True)
    ).all()
    if vencidos:
        db.execute(delete(Lancamento).where(Lancamento.extrato_id.in_(vencidos)))
        db.execute(delete(Extrato).where(Extrato.id.in_(vencidos)))
    return len(vencidos)


def criar_rascunho(
    db: Session,
    empresa_id: uuid.UUID,
    nome_arquivo: str,
    tamanho_bytes: int,
    lancamentos: list[LancamentoImportado],
) -> Extrato:
    """Grava o rascunho e os lançamentos, sem commit."""
    datas = [lancamento.data for lancamento in lancamentos]
    extrato = Extrato(
        id=uuid.uuid4(),
        empresa_id=empresa_id,
        nome_arquivo=nome_arquivo,
        formato="json",
        tamanho_bytes=tamanho_bytes,
        origem="sistema",
        status=STATUS_RASCUNHO,
        quantidade_lancamentos=len(lancamentos),
        periodo_inicio=min(datas),
        periodo_fim=max(datas),
    )
    db.add(extrato)
    db.flush()

    # Mesma numeração da normalização: a ocorrência de cada linha idêntica
    # (valor, data, descrição normalizada), na ordem do arquivo.
    contagem: dict[tuple, int] = {}
    valores = []
    for lancamento in lancamentos:
        chave = (lancamento.valor, lancamento.data, _normalizar_descricao(lancamento.descricao))
        contagem[chave] = ocorrencia = contagem.get(chave, 0) + 1
        valores.append(
            {
                "id": uuid.uuid4(),
                "empresa_id": empresa_id,
                "extrato_id": extrato.id,
                "data": lancamento.data,
                "valor": lancamento.valor,
                "descricao": lancamento.descricao,
                "tipo": lancamento.tipo,
                "hash_dedup": _calcular_hash_dedup(
                    lancamento.valor, lancamento.data, lancamento.descricao, extrato.id, ocorrencia
                ),
                "ocorrencia": ocorrencia,
                "posicao": lancamento.posicao,
            }
        )
    for inicio in range(0, len(valores), TAMANHO_BLOCO_INSERT):
        db.execute(insert(Lancamento).values(valores[inicio : inicio + TAMANHO_BLOCO_INSERT]))
    return extrato


def rascunho_da_empresa(
    db: Session, empresa_id: uuid.UUID, rascunho_id: uuid.UUID, *, travar: bool = False
) -> Extrato | None:
    """O rascunho vigente da empresa, ou `None` (inexistente, de outra
    empresa, vencido ou já confirmado). Com `travar`, `FOR UPDATE`: o
    descarte e a confirmação não correm um contra o outro."""
    consulta = select(Extrato).where(
        Extrato.id == rascunho_id,
        Extrato.empresa_id == empresa_id,
        Extrato.status == STATUS_RASCUNHO,
        _vigente(),
    )
    if travar:
        consulta = consulta.with_for_update()
    return db.scalars(consulta).one_or_none()


def descartar(db: Session, rascunho: Extrato) -> None:
    """Apaga os lançamentos e o rascunho, sem commit."""
    db.execute(delete(Lancamento).where(Lancamento.extrato_id == rascunho.id))
    db.execute(delete(Extrato).where(Extrato.id == rascunho.id))
