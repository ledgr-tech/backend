"""`POST /explicacoes` (issue #28, ADR-011): explicação em linguagem
natural de por que um lançamento aparece como divergência, com cache.

Exige JWT (app/core/auth.py, ADR-003): `empresa_id` sempre vem do token.
Corpo `{"conciliacao_id": "<uuid>"}` — UUID malformado é 422 do próprio
Pydantic. Fluxo, nesta ordem:

(a) Busca a linha de `conciliacoes` por id E `empresa_id` do token —
inexistente ou de outra empresa devolve 404 com a MESMA mensagem (sem
confirmar a existência do id de outra empresa, mesmo padrão de
app/api/conciliacoes.py). (b)/(c) `montar_contexto_divergencia`
(app/services/ia/contexto.py) já valida elegibilidade (levanta
`ValueError` pra `match_exato`/`match_tolerancia`/desconhecido, traduzido
aqui pra 422) e monta o contexto. (d) Provedor indisponível
(`obter_provedor()` devolveu `None`: recurso desligado, sem chave ou
provedor desconhecido) — 200 com o fallback determinístico,
`indisponibilidade="desabilitado"`. Isso vale mesmo com texto em cache: a
flag desligada é o interruptor de emergência, nenhum texto de IA pode
sair com o recurso desligado. (e) Cache hit (mesma `empresa_id` + `chave`,
app/services/ia/cache.py) devolve o texto gravado, sem gastar nada e sem
contar pro limite. (f) Limite diário (contado em `explicacoes_divergencia`
por `criado_em >= hoje UTC`, só geração nova conta) — por empresa ou
global, fallback com `indisponibilidade="limite_diario"`, sem chamar o
provedor. (g) Gera, com `identificador_anonimo` = sha256 hex do
`str(empresa_id)` — o id cru nunca vai ao provedor. (h)
`ProvedorIAIndisponivel` (qualquer motivo) ou qualquer outra `Exception`
da chamada: fallback com `indisponibilidade="erro_provedor"`, nada é
gravado, a próxima chamada tenta de novo. (i) Sucesso: grava com `INSERT
... ON CONFLICT (empresa_id, chave) DO NOTHING`; se o INSERT falhar por
erro de banco, rollback e devolve o texto gerado mesmo assim
(`em_cache=False`).

Nenhuma transação de banco fica aberta durante a chamada HTTP ao provedor
(passo g): todas as leituras (linha, contexto, cache, contagens) terminam
e a transação é encerrada (`db.rollback()`, já que só leu) antes de
`provedor.explicar_divergencia(...)`; só depois disso abre outra pro
INSERT do passo (i).

Consequência importante: `rollback()`/`commit()` expiram os objetos ORM
carregados na sessão, então qualquer acesso a atributo deles DEPOIS disso
dispara um SELECT novo por id — e `POST /conciliacoes` apaga e reescreve
as linhas do par a cada rodada (ADR-007), então a linha pode não existir
mais se o par for reconciliado durante a chamada ao provedor. Por isso
`status_conciliacao` (e, no cache hit, o texto do cache) são copiados pra
variável local logo após a leitura, antes do primeiro `rollback()` — nada
mais no corpo da função lê atributo de `conciliacao`/`cache_existente`
depois disso.

O provedor entra por dependência do FastAPI (`obter_provedor_dependencia`)
pra testes substituírem por um provedor falso via
`app.dependency_overrides`, sem chamar a API real.

Nunca loga prompt, texto da explicação, descrição de lançamento nem a
chave — só o que já é logado em app/services/ia/openai_provider.py
(provedor, modelo, status HTTP, duração, tokens), mais o motivo de
indisponibilidade (categoria) e, em erro inesperado ou falha de INSERT, só
o nome da classe da exceção.
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.auth import obter_empresa_id_autenticada
from app.core.config import settings
from app.core.database import get_db
from app.models import Conciliacao, ExplicacaoDivergencia
from app.services.ia import ProvedorDeIA, ProvedorIAIndisponivel, obter_provedor
from app.services.ia.cache import chave_cache
from app.services.ia.contexto import montar_contexto_divergencia
from app.services.ia.prompt import VERSAO_PROMPT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/explicacoes", tags=["explicacoes"])


class ExplicacaoRequest(BaseModel):
    conciliacao_id: uuid.UUID


class ExplicacaoResponse(BaseModel):
    conciliacao_id: uuid.UUID
    status: str
    explicacao: str
    gerada_por_ia: bool
    em_cache: bool
    indisponibilidade: Literal["desabilitado", "erro_provedor", "limite_diario"] | None


def obter_provedor_dependencia() -> ProvedorDeIA | None:
    return obter_provedor()


def _inicio_do_dia_utc() -> datetime:
    agora = datetime.now(UTC)
    return agora.replace(hour=0, minute=0, second=0, microsecond=0)


@router.post("", response_model=ExplicacaoResponse)
def criar_explicacao(
    corpo: ExplicacaoRequest,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeIA | None, Depends(obter_provedor_dependencia)],
) -> ExplicacaoResponse:
    conciliacao = db.execute(
        select(Conciliacao).where(
            Conciliacao.id == corpo.conciliacao_id,
            Conciliacao.empresa_id == empresa_id,
        )
    ).scalar_one_or_none()
    if conciliacao is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Conciliação não encontrada.",
        )

    # Captura tudo que o resto da função precisa da linha, antes de
    # qualquer rollback/commit — a sessão expira os objetos por padrão em
    # ambos, e um acesso a atributo depois disso dispara um SELECT novo por
    # id. Como POST /conciliacoes apaga e reescreve as linhas do par a cada
    # rodada, se o par for reconciliado durante a chamada ao provedor essa
    # linha pode não existir mais, e o refresh levantaria
    # `ObjectDeletedError` (500) depois da explicação já ter sido gerada
    # (e paga). Daqui em diante, nada mais lê atributo de `conciliacao`.
    status_conciliacao = conciliacao.status

    try:
        contexto = montar_contexto_divergencia(db, conciliacao)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if provedor is None:
        db.rollback()
        return ExplicacaoResponse(
            conciliacao_id=corpo.conciliacao_id,
            status=status_conciliacao,
            explicacao=contexto.motivo,
            gerada_por_ia=False,
            em_cache=False,
            indisponibilidade="desabilitado",
        )

    chave = chave_cache(empresa_id, settings.llm_provedor, settings.openai_modelo, contexto)

    cache_existente = db.execute(
        select(ExplicacaoDivergencia).where(
            ExplicacaoDivergencia.empresa_id == empresa_id,
            ExplicacaoDivergencia.chave == chave,
        )
    ).scalar_one_or_none()
    if cache_existente is not None:
        texto_cache = cache_existente.texto  # antes do rollback, mesmo motivo do comentário acima
        db.rollback()
        return ExplicacaoResponse(
            conciliacao_id=corpo.conciliacao_id,
            status=status_conciliacao,
            explicacao=texto_cache,
            gerada_por_ia=True,
            em_cache=True,
            indisponibilidade=None,
        )

    hoje_utc = _inicio_do_dia_utc()
    contagem_empresa = db.scalar(
        select(func.count())
        .select_from(ExplicacaoDivergencia)
        .where(
            ExplicacaoDivergencia.empresa_id == empresa_id,
            ExplicacaoDivergencia.criado_em >= hoje_utc,
        )
    )
    contagem_global = db.scalar(
        select(func.count())
        .select_from(ExplicacaoDivergencia)
        .where(ExplicacaoDivergencia.criado_em >= hoje_utc)
    )
    if (
        contagem_empresa >= settings.llm_limite_diario_empresa
        or contagem_global >= settings.llm_limite_diario_global
    ):
        db.rollback()
        return ExplicacaoResponse(
            conciliacao_id=corpo.conciliacao_id,
            status=status_conciliacao,
            explicacao=contexto.motivo,
            gerada_por_ia=False,
            em_cache=False,
            indisponibilidade="limite_diario",
        )

    # Fim das leituras: encerra a transação (só leu, então rollback) antes
    # de chamar o provedor — nenhuma transação de banco fica aberta durante
    # a chamada HTTP externa.
    db.rollback()

    identificador_anonimo = hashlib.sha256(str(empresa_id).encode()).hexdigest()
    try:
        resposta_ia = provedor.explicar_divergencia(
            contexto, identificador_anonimo=identificador_anonimo
        )
    except ProvedorIAIndisponivel as exc:
        logger.info("explicacoes motivo=%s status_http=%s", exc.motivo, exc.status_http)
        return ExplicacaoResponse(
            conciliacao_id=corpo.conciliacao_id,
            status=status_conciliacao,
            explicacao=contexto.motivo,
            gerada_por_ia=False,
            em_cache=False,
            indisponibilidade="erro_provedor",
        )
    except Exception as exc:  # noqa: BLE001 - qualquer falha do provedor vira fallback
        logger.info("explicacoes erro_inesperado classe=%s", type(exc).__name__)
        return ExplicacaoResponse(
            conciliacao_id=corpo.conciliacao_id,
            status=status_conciliacao,
            explicacao=contexto.motivo,
            gerada_por_ia=False,
            em_cache=False,
            indisponibilidade="erro_provedor",
        )

    try:
        db.execute(
            pg_insert(ExplicacaoDivergencia.__table__)
            .values(
                id=uuid.uuid4(),
                empresa_id=empresa_id,
                chave=chave,
                status=status_conciliacao,
                provedor=settings.llm_provedor,
                modelo=settings.openai_modelo,
                versao_prompt=VERSAO_PROMPT,
                texto=resposta_ia.texto,
                tokens_entrada=resposta_ia.tokens_entrada,
                tokens_saida=resposta_ia.tokens_saida,
            )
            .on_conflict_do_nothing(index_elements=["empresa_id", "chave"])
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - grava o que puder, nunca perde a explicação
        db.rollback()
        logger.info("explicacoes cache_insert_falhou classe=%s", type(exc).__name__)

    return ExplicacaoResponse(
        conciliacao_id=corpo.conciliacao_id,
        status=status_conciliacao,
        explicacao=resposta_ia.texto,
        gerada_por_ia=True,
        em_cache=False,
        indisponibilidade=None,
    )
