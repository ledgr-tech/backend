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

Revisão (issue #159): `resumo`, `pagina`, `corrigir`, `remover`, `adicionar`
e `confirmar`. Quem chama trava o rascunho (`rascunho_da_empresa` com
`travar`), e todas as escritas de um rascunho passam por essa trava, então
a confirmação e o descarte não correm contra uma correção.

- **`ocorrencia` livre na edição.** A linha corrigida que muda de chave
  (valor, data e descrição normalizada) e a linha nova recebem
  `ocorrencia` = 1 + a maior da mesma chave no rascunho, e o `hash_dedup`
  dela. Nunca colide com outra linha, e a UNIQUE `(empresa_id, extrato_id,
  hash_dedup)` vale em todo passo. Corrigir sem mudar de chave mantém a
  ocorrência.
- **Renumeração na confirmação.** `ocorrencia` volta a 1, 2... por chave na
  ordem de `posicao`, como na normalização de um arquivo, e o hash é
  refeito. Em duas fases, porque a UNIQUE não pode ser adiável (o
  `ON CONFLICT` da normalização não aceita constraint adiável): primeiro as
  linhas que mudam recebem um hash provisório único (o id sem hífens, 32
  caracteres, que não colide com um sha256 de 64), depois o definitivo. A
  linha que não muda não é tocada.
- A confirmação recalcula quantidade e período, passa o status para
  "concluido" e grava quem confirmou e quando. Daí em diante é um extrato do
  sistema como os outros: aparece em `GET /extratos` e entra na conciliação.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.models import Extrato, Lancamento, Usuario
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


def importacao_confirmada(db: Session, empresa_id: uuid.UUID, importacao_id: uuid.UUID) -> bool:
    """A importação via IA desta empresa já virou extrato (issue #159): o 409
    do duplo clique, e não o 404."""
    return (
        db.scalar(
            select(Extrato.id).where(
                Extrato.id == importacao_id,
                Extrato.empresa_id == empresa_id,
                Extrato.formato == "json",
                Extrato.status != STATUS_RASCUNHO,
            )
        )
        is not None
    )


ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Resumo:
    quantidade: int
    total_entradas: Decimal
    total_saidas: Decimal
    periodo_inicio: date | None
    periodo_fim: date | None
    expira_em: datetime


def resumo(db: Session, rascunho: Extrato) -> Resumo:
    """Quantidade, soma das entradas, soma das saídas (negativa) e período do
    rascunho, numa consulta. Sem linhas, as somas são zero e o período, nulo."""
    linha = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((Lancamento.valor > 0, Lancamento.valor))), 0),
            func.coalesce(func.sum(case((Lancamento.valor < 0, Lancamento.valor))), 0),
            func.min(Lancamento.data),
            func.max(Lancamento.data),
        ).where(Lancamento.extrato_id == rascunho.id)
    ).one()
    return Resumo(
        quantidade=linha[0],
        total_entradas=Decimal(linha[1]).quantize(ZERO),
        total_saidas=Decimal(linha[2]).quantize(ZERO),
        periodo_inicio=linha[3],
        periodo_fim=linha[4],
        expira_em=(rascunho.criado_em + VALIDADE).replace(tzinfo=UTC),
    )


def pagina(
    db: Session, rascunho: Extrato, limit: int, offset: int
) -> tuple[int, list[tuple[int, Lancamento]]]:
    """O total e as linhas da página na ordem de `posicao`, cada uma com o
    número (a partir de 1) na ordem atual."""
    total = db.scalar(
        select(func.count()).select_from(Lancamento).where(Lancamento.extrato_id == rascunho.id)
    )
    linhas = db.scalars(
        select(Lancamento)
        .where(Lancamento.extrato_id == rascunho.id)
        .order_by(Lancamento.posicao, Lancamento.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return total, [(offset + i + 1, linha) for i, linha in enumerate(linhas)]


def lancamento_do_rascunho(
    db: Session, rascunho: Extrato, lancamento_id: uuid.UUID
) -> Lancamento | None:
    return db.scalars(
        select(Lancamento).where(
            Lancamento.id == lancamento_id, Lancamento.extrato_id == rascunho.id
        )
    ).one_or_none()


def numero_do_lancamento(db: Session, lancamento: Lancamento) -> int:
    """O número da linha na ordem atual do rascunho, a partir de 1."""
    return 1 + db.scalar(
        select(func.count())
        .select_from(Lancamento)
        .where(
            Lancamento.extrato_id == lancamento.extrato_id,
            Lancamento.posicao < lancamento.posicao,
        )
    )


def _chave(valor: Decimal, data_lancamento: date, descricao: str) -> tuple:
    return (valor, data_lancamento, _normalizar_descricao(descricao))


def _ocorrencia_livre(
    db: Session,
    rascunho: Extrato,
    chave: tuple,
    sem: uuid.UUID | None = None,
) -> int:
    """1 + a maior `ocorrencia` da mesma chave no rascunho. A descrição
    normalizada não sai do banco igual, então busca pelo valor e pela data
    (com índice) e compara a descrição aqui."""
    valor, data_lancamento, descricao = chave
    candidatas = db.execute(
        select(Lancamento.id, Lancamento.descricao, Lancamento.ocorrencia).where(
            Lancamento.extrato_id == rascunho.id,
            Lancamento.valor == valor,
            Lancamento.data == data_lancamento,
        )
    ).all()
    ocorrencias = [
        linha.ocorrencia
        for linha in candidatas
        if linha.id != sem and _normalizar_descricao(linha.descricao) == descricao
    ]
    return 1 + max(ocorrencias, default=0)


def corrigir(db: Session, rascunho: Extrato, lancamento: Lancamento, campos: dict) -> None:
    """Aplica os campos já validados, sem commit. Mudou de chave: ocorrência
    livre e hash novo."""
    antes = _chave(lancamento.valor, lancamento.data, lancamento.descricao)
    for nome, valor in campos.items():
        setattr(lancamento, nome, valor)
    lancamento.tipo = "credito" if lancamento.valor >= 0 else "debito"
    depois = _chave(lancamento.valor, lancamento.data, lancamento.descricao)
    if depois != antes:
        lancamento.ocorrencia = _ocorrencia_livre(db, rascunho, depois, sem=lancamento.id)
    lancamento.hash_dedup = _calcular_hash_dedup(
        lancamento.valor, lancamento.data, lancamento.descricao, rascunho.id, lancamento.ocorrencia
    )
    db.flush()


def remover(db: Session, lancamento: Lancamento) -> None:
    db.execute(delete(Lancamento).where(Lancamento.id == lancamento.id))


def adicionar(db: Session, rascunho: Extrato, campos: dict) -> Lancamento:
    """Linha nova no fim da ordem, com ocorrência livre, sem commit."""
    ultima = db.scalar(
        select(func.max(Lancamento.posicao)).where(Lancamento.extrato_id == rascunho.id)
    )
    chave = _chave(campos["valor"], campos["data"], campos["descricao"])
    ocorrencia = _ocorrencia_livre(db, rascunho, chave)
    lancamento = Lancamento(
        id=uuid.uuid4(),
        empresa_id=rascunho.empresa_id,
        extrato_id=rascunho.id,
        data=campos["data"],
        valor=campos["valor"],
        descricao=campos["descricao"],
        tipo="credito" if campos["valor"] >= 0 else "debito",
        hash_dedup=_calcular_hash_dedup(
            campos["valor"], campos["data"], campos["descricao"], rascunho.id, ocorrencia
        ),
        ocorrencia=ocorrencia,
        posicao=0 if ultima is None else ultima + 1,
    )
    db.add(lancamento)
    db.flush()
    return lancamento


def confirmar(db: Session, rascunho: Extrato, usuario: Usuario) -> None:
    """Renumera, refaz o hash, recalcula quantidade e período e passa o
    rascunho a extrato concluído, sem commit. Quem chama confere que há
    pelo menos uma linha."""
    linhas = db.execute(
        select(
            Lancamento.id,
            Lancamento.valor,
            Lancamento.data,
            Lancamento.descricao,
            Lancamento.ocorrencia,
            Lancamento.hash_dedup,
        )
        .where(Lancamento.extrato_id == rascunho.id)
        .order_by(Lancamento.posicao, Lancamento.id)
    ).all()
    contagem: dict[tuple, int] = {}
    mudam = []
    for linha in linhas:
        chave = _chave(linha.valor, linha.data, linha.descricao)
        contagem[chave] = ocorrencia = contagem.get(chave, 0) + 1
        hash_dedup = _calcular_hash_dedup(
            linha.valor, linha.data, linha.descricao, rascunho.id, ocorrencia
        )
        if (ocorrencia, hash_dedup) != (linha.ocorrencia, linha.hash_dedup):
            mudam.append({"id": linha.id, "ocorrencia": ocorrencia, "hash_dedup": hash_dedup})
    if mudam:
        # Fase 1: hash provisório único; fase 2: o definitivo (docstring do módulo).
        db.execute(
            update(Lancamento),
            [{"id": item["id"], "hash_dedup": item["id"].hex} for item in mudam],
        )
        db.execute(update(Lancamento), mudam)

    datas = [linha.data for linha in linhas]
    rascunho.status = "concluido"
    rascunho.quantidade_lancamentos = len(linhas)
    rascunho.periodo_inicio = min(datas)
    rascunho.periodo_fim = max(datas)
    rascunho.confirmado_por_id = usuario.id
    rascunho.confirmado_por_nome = usuario.nome
    rascunho.confirmado_em = func.now()
    db.flush()
