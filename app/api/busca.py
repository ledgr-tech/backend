"""`GET /busca` (issue #220): a busca do topo do app (Ctrl K) nos lançamentos
de todas as conciliações da empresa ativa. Regras da busca em
app/services/busca.py.

Exige JWT e vale para a empresa ativa (`X-Ledgr-Empresa`, app/core/auth.py),
qualquer papel: é só leitura, como a listagem das conciliações.

Parâmetros:

- `q`: o termo, de 1 a 100 caracteres e com algum que não seja espaço (senão
  422). Lido como valor, data e texto;
- `limit` de 1 a 50 (padrão 10) e `offset` a partir de 0. `offset` além do
  total dá 200 com a lista vazia e o total, como nas outras listagens.

Cada item é uma linha da conciliação, com o que o front precisa para abri-la:
`conciliacao_id`, `extrato_banco_id`, `extrato_sistema_id`, `chave` (a mesma
da listagem de `GET /conciliacoes/{extrato_id}` e das decisões), `status`,
`competencia` (AAAA-MM do `periodo_inicio` do extrato do banco, `null` sem
período) e os dois lançamentos, no formato da listagem (`null` no lado que
falta). Valores em string, como no resto da API.

Limite de `LIMITE_BUSCA_POR_USUARIO` por usuário.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.conciliacoes import LancamentoResponse
from app.core.auth import obter_empresa_id_ativa
from app.core.database import get_db
from app.core.rate_limit import LIMITE_BUSCA_POR_USUARIO, chave_por_usuario, limiter
from app.services import busca, decisoes

router = APIRouter(prefix="/busca", tags=["busca"])


class ItemBuscaResponse(BaseModel):
    conciliacao_id: uuid.UUID
    extrato_banco_id: uuid.UUID
    extrato_sistema_id: uuid.UUID
    chave: str
    status: str
    competencia: str | None
    lancamento_banco: LancamentoResponse | None
    lancamento_sistema: LancamentoResponse | None


class BuscaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[ItemBuscaResponse]


def _lancamento(linha, prefixo: str) -> LancamentoResponse | None:
    id_ = getattr(linha, f"{prefixo}_id")
    if id_ is None:
        return None
    return LancamentoResponse(
        id=id_,
        data=getattr(linha, f"{prefixo}_data"),
        valor=getattr(linha, f"{prefixo}_valor"),
        descricao=getattr(linha, f"{prefixo}_descricao"),
        tipo=getattr(linha, f"{prefixo}_tipo"),
    )


def _item(linha) -> ItemBuscaResponse:
    return ItemBuscaResponse(
        conciliacao_id=linha.id,
        extrato_banco_id=linha.extrato_banco_id,
        extrato_sistema_id=linha.extrato_sistema_id,
        chave=decisoes.chave_da_linha(
            linha.banco_id,
            linha.sistema_valor,
            linha.sistema_data,
            linha.sistema_descricao,
            linha.sistema_ocorrencia,
        ),
        status=linha.status,
        competencia=linha.competencia,
        lancamento_banco=_lancamento(linha, "banco"),
        lancamento_sistema=_lancamento(linha, "sistema"),
    )


@router.get("", response_model=BuscaResponse)
@limiter.limit(LIMITE_BUSCA_POR_USUARIO, key_func=chave_por_usuario)
def buscar(
    request: Request,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=100, pattern=r"\S")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BuscaResponse:
    vazia = BuscaResponse(total=0, limit=limit, offset=offset, itens=[])
    termo = busca.interpretar_termo(q)
    if termo.vazio:
        return vazia
    pares = busca.pares_em_vigor(db, empresa_id)
    if not pares:
        return vazia
    consulta = busca.consulta_da_busca(empresa_id, pares, termo)
    linhas = db.execute(consulta.limit(limit).offset(offset)).all()
    total = linhas[0].total if linhas else busca.contar(db, consulta)
    return BuscaResponse(
        total=total, limit=limit, offset=offset, itens=[_item(linha) for linha in linhas]
    )
