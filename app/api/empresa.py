"""Configurações da empresa (issue #85): tolerância de dias editável.

`GET /empresa/configuracoes` lê e `PUT /empresa/configuracoes` altera a
tolerância de data que o motor de matching aplica (ADR-008). Sempre pela
empresa do token, nunca por parâmetro. Conta única no MVP: não há papéis nem
403, qualquer usuário da empresa altera (decisão de 05/10).

Só a tolerância em DIAS é exposta (decisão de 05/10): tolerância de valor e
semelhança mínima contrariam a ADR-006 e a ADR-008, e
`similaridade_minima_default` continua só desempatando. O corpo do `PUT`
recusa qualquer outro campo com 422, para o front nunca achar que salvou algo
que o backend não guarda. Os 422 deste `PUT` são o padrão do FastAPI (`detail`
em lista, com `loc: ["body", "tolerancia_dias"]`), que é o que a spec do
front pede.

Empresa sem linha em `configuracoes` lê 0, igual ao motor, e o `GET` não cria
a linha; o `PUT` cria (upsert, sem corrida entre dois `PUT` simultâneos).

O motor não muda: `conciliar_extratos` lê a tolerância a cada execução, então
a próxima conciliação já usa o valor novo, e uma conciliação em andamento
termina com o valor que leu. Cada execução continua registrando a tolerância
aplicada em `execucoes_conciliacao.tolerancia_dias`.

`atualizado_por` e `atualizado_em` só vêm preenchidos depois que alguém
alterou pela rota; antes, os dois são `null`. `atualizado_em` sai em UTC
explícito (a coluna é `TIMESTAMP` sem fuso, gravada em UTC), como o
`executada_em` de app/api/execucoes.py.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.auth import obter_empresa_id_autenticada, obter_usuario_autenticado
from app.core.database import get_db
from app.models import Configuracao, Usuario

router = APIRouter(prefix="/empresa", tags=["empresa"])

# Decisão de Eduardo em 05/10: a tolerância de dias vai de 0 a 5. O limite é
# regra de produto e fica só aqui (o banco garante apenas o mínimo).
TOLERANCIA_DIAS_MAXIMA = 5


class ConfiguracoesResponse(BaseModel):
    tolerancia_dias: int
    tolerancia_dias_maximo: int
    atualizado_por: str | None
    atualizado_em: datetime | None


class ConfiguracoesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tolerancia_dias: Annotated[int, Field(ge=0, le=TOLERANCIA_DIAS_MAXIMA, strict=True)]


def _resposta(configuracao: Configuracao | None) -> ConfiguracoesResponse:
    if configuracao is None:
        return ConfiguracoesResponse(
            tolerancia_dias=0,
            tolerancia_dias_maximo=TOLERANCIA_DIAS_MAXIMA,
            atualizado_por=None,
            atualizado_em=None,
        )
    alterada_pela_rota = configuracao.atualizado_por_nome is not None
    return ConfiguracoesResponse(
        tolerancia_dias=configuracao.tolerancia_dias_default,
        tolerancia_dias_maximo=TOLERANCIA_DIAS_MAXIMA,
        atualizado_por=configuracao.atualizado_por_nome if alterada_pela_rota else None,
        atualizado_em=(
            configuracao.atualizado_em.replace(tzinfo=UTC) if alterada_pela_rota else None
        ),
    )


def _configuracao_da_empresa(db: Session, empresa_id: uuid.UUID) -> Configuracao | None:
    return db.scalar(select(Configuracao).where(Configuracao.empresa_id == empresa_id))


@router.get("/configuracoes", response_model=ConfiguracoesResponse)
def obter_configuracoes(
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
) -> ConfiguracoesResponse:
    return _resposta(_configuracao_da_empresa(db, empresa_id))


@router.put("/configuracoes", response_model=ConfiguracoesResponse)
def alterar_configuracoes(
    corpo: ConfiguracoesRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> ConfiguracoesResponse:
    empresa_id = usuario.empresa_id
    alteracao = {
        "tolerancia_dias_default": corpo.tolerancia_dias,
        "atualizado_por_id": usuario.id,
        "atualizado_por_nome": usuario.nome,
    }
    db.execute(
        pg_insert(Configuracao)
        .values(id=uuid.uuid4(), empresa_id=empresa_id, **alteracao)
        .on_conflict_do_update(
            index_elements=[Configuracao.empresa_id],
            set_={**alteracao, "atualizado_em": func.now()},
        )
    )
    db.commit()
    return _resposta(_configuracao_da_empresa(db, empresa_id))
