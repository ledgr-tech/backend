"""`POST /explicacoes` (issue #28, ADR-011): explicação em linguagem
natural de por que um lançamento aparece como divergência, com cache.

Exige JWT (app/core/auth.py, ADR-003): `empresa_id` é sempre o da empresa
ativa (cabeçalho `X-Ledgr-Empresa`, conferido contra a conta do token desde a
issue #133).
Corpo `{"conciliacao_id": "<uuid>"}` — UUID malformado é 422 do próprio
Pydantic. Fluxo, nesta ordem:

(a) Busca a linha de `conciliacoes` por id E `empresa_id` do token —
inexistente ou de outra empresa devolve 404 com a MESMA mensagem (sem
confirmar a existência do id de outra empresa, mesmo padrão de
app/api/conciliacoes.py). (b)/(c) `montar_contexto_divergencia`
(app/services/ia/contexto.py) já valida elegibilidade (levanta
`ValueError` pra `match_exato`/`match_tolerancia`/`match_agrupado`/desconhecido, traduzido
aqui pra 422) e monta o contexto. (d) Provedor indisponível
(`obter_provedor()` devolveu `None`: recurso desligado, sem chave ou
provedor desconhecido) — 200 com o fallback determinístico,
`indisponibilidade="desabilitado"`. Isso vale mesmo com texto em cache: a
flag desligada é o interruptor de emergência, nenhum texto de IA pode
sair com o recurso desligado. (e) Cache hit (mesma `empresa_id` + `chave`,
app/services/ia/cache.py) devolve o texto gravado, sem gastar nada e sem
contar pro limite. (f) Limite diário (contado em `explicacoes_divergencia`
por `criado_em >= hoje UTC`, só geração nova conta) — por CONTA desde a
issue #140 (todas as empresas clientes dela, `LLM_LIMITE_DIARIO_CONTA`) ou
global, fallback com `indisponibilidade="limite_diario"`, sem chamar o
provedor. (g) Gera, com `identificador_anonimo` = sha256 hex do
`str(empresa_id)` — o id cru nunca vai ao provedor. (h)
`ProvedorIAIndisponivel` (qualquer motivo) ou qualquer outra `Exception`
da chamada: fallback com `indisponibilidade="erro_provedor"`, nada é
gravado, a próxima chamada tenta de novo. O motivo `cota_esgotada` (teto
de gasto da OpenAI, issue #87) sai no log em WARNING; os outros, em INFO.
(i) Sucesso: grava com `INSERT ... ON CONFLICT (empresa_id, chave) DO
NOTHING`; se o INSERT falhar por erro de banco, rollback e devolve o texto
gerado mesmo assim (`em_cache=False`).

Desde a issue #140, a regra mora em app/services/ia/geracao.py, a mesma da
geração automática ao conciliar, e o contrato desta rota não mudou. Quando a
rota gera ou acha no cache um texto do modelo, liga a linha a ele
(`conciliacoes.explicacao_chave`), e o texto do clique passa a aparecer no
`GET /conciliacoes/{id}`.

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

Detalhes (fix da explicação detalhada): a resposta do POST leva
`detalhes`, as linhas já formatadas do contexto (`rotulo` e `valor`, em
pt-BR, sem descrição; app/services/ia/detalhes.py), em todos os caminhos:
IA ligada, desligada, limite, erro e cache. No fallback, `explicacao` é o
texto determinístico com os números (`texto_deterministico`), não mais o
motivo genérico; com a IA respondendo, continua o texto da IA. O texto da
IA é conferido contra os fatos antes de gravar (prompt v3): data, valor ou
número de dias que não está nos fatos descarta o texto, e a resposta vira o
fallback com `erro_provedor`.

`GET /explicacoes/{conciliacao_id}/detalhes`: somente leitura, devolve
`{conciliacao_id, status, texto, detalhes}` com o texto determinístico. Não
chama o provedor, não conta no limite diário, não grava nada (nem a ligação
da linha) e funciona com `LLM_HABILITADO` desligado. Mesmas dependências,
mesmo 404 (inexistente ou de outra empresa) e mesmo 422 (match) do POST.
Sem rate limit próprio, como o POST e as rotas de conciliação.

O provedor entra por dependência do FastAPI (`obter_provedor_dependencia`)
pra testes substituírem por um provedor falso via
`app.dependency_overrides`, sem chamar a API real.

Nunca loga prompt, texto da explicação, descrição de lançamento nem a
chave — só o que já é logado em app/services/ia/openai_provider.py
(provedor, modelo, status HTTP, duração, tokens), mais o motivo de
indisponibilidade (categoria) e, em erro inesperado ou falha de INSERT, só
o nome da classe da exceção.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import obter_empresa_id_ativa
from app.core.database import get_db
from app.services.ia import ProvedorDeIA
from app.services.ia.detalhes import DetalheLinha, montar_detalhes, texto_deterministico
from app.services.ia.geracao import (
    LinhaNaoEncontrada,
    explicar_linha,
    obter_provedor_dependencia,
    preparar,
)

__all__ = ["obter_provedor_dependencia", "router"]


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
    detalhes: list[DetalheLinha]


class DetalhesResponse(BaseModel):
    conciliacao_id: uuid.UUID
    status: str
    texto: str
    detalhes: list[DetalheLinha]


_NAO_ENCONTRADA = "Conciliação não encontrada."


@router.post("", response_model=ExplicacaoResponse)
def criar_explicacao(
    corpo: ExplicacaoRequest,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeIA | None, Depends(obter_provedor_dependencia)],
) -> ExplicacaoResponse:
    try:
        resultado = explicar_linha(db, empresa_id, corpo.conciliacao_id, provedor)
    except LinhaNaoEncontrada as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=_NAO_ENCONTRADA,
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return ExplicacaoResponse(conciliacao_id=corpo.conciliacao_id, **vars(resultado))


@router.get("/{conciliacao_id}/detalhes", response_model=DetalhesResponse)
def obter_detalhes(
    conciliacao_id: uuid.UUID,
    empresa_id: Annotated[uuid.UUID, Depends(obter_empresa_id_ativa)],
    db: Annotated[Session, Depends(get_db)],
) -> DetalhesResponse:
    try:
        preparo = preparar(db, empresa_id, conciliacao_id, com_cache=False)
    except LinhaNaoEncontrada as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    finally:
        db.rollback()  # só leitura
    return DetalhesResponse(
        conciliacao_id=conciliacao_id,
        status=preparo.status,
        texto=texto_deterministico(preparo.contexto),
        detalhes=montar_detalhes(preparo.contexto),
    )
