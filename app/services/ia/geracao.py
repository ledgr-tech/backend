"""Geração da explicação de uma divergência (issues #28 e #140, ADR-011).

Uma regra só, usada pelo `POST /explicacoes` (o clique) e pela geração
automática ao conciliar, para as duas nunca divergirem:

1. `preparar`: lê a linha de `conciliacoes` pela empresa, monta o contexto
   mascarado (`montar_contexto_divergencia`, que recusa match com
   `ValueError`) e, se pedido, calcula a chave do cache e procura o texto.
2. Cache hit: devolve o texto e liga a linha a ele, sem gastar nem contar no
   limite.
3. Limite diário por CONTA (todas as empresas clientes dela) e global,
   contados em `explicacoes_divergencia` desde a meia-noite UTC.
4. A transação é encerrada antes da chamada ao provedor: nenhuma fica aberta
   durante a chamada HTTP. O identificador anônimo é o sha256 do
   `empresa_id`; o id cru nunca vai ao provedor.
5. `ProvedorIAIndisponivel` ou qualquer outra exceção da chamada viram o
   fallback `erro_provedor`, e nada é gravado.
6. Sucesso: `INSERT ... ON CONFLICT (empresa_id, chave) DO NOTHING` e a
   ligação da linha (`conciliacoes.explicacao_chave`), na mesma transação.
   Se gravar falhar, o texto gerado é devolvido assim mesmo. Se a linha
   sumiu (o par foi reconciliado no meio, ADR-007), o texto fica no cache e
   a ligação simplesmente não acontece.

Os objetos ORM expiram no rollback e no commit, e a linha pode ser apagada
por uma reconciliação durante a chamada: por isso `preparar` copia o que
precisa (status e contexto) para um `Preparo` antes de encerrar a leitura.

Geração automática (`gerar_explicacoes_do_par`, issue #140): roda numa
`BackgroundTask` do `POST /conciliacoes`, depois da resposta, só com
`EXPLICACAO_AUTOMATICA_HABILITADA` e `LLM_HABILITADO` ligadas. Abre a própria
sessão; prepara as linhas divergentes do par em ordem de valor absoluto (do
lançamento do banco ou, sem ele, do sistema) e chave, liga os cache hits e
gera até `LLM_TETO_POR_CONCILIACAO` explicações novas, com
`LLM_CONCORRENCIA_AUTOMATICA` chamadas ao mesmo tempo e uma sessão por thread.
Linhas com a mesma chave são geradas uma vez. Qualquer falha de uma linha
deixa só ela sem explicação. A tarefa nunca relança exceção. Com
concorrência, o limite diário pode passar por no máximo a concorrência,
porque cada thread conta antes de chamar.

Nunca loga prompt, texto, descrição nem chave: só categorias e contagens.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Conciliacao, Empresa, ExplicacaoDivergencia, Lancamento
from app.services.decisoes import chave_da_linha
from app.services.ia import ProvedorDeIA, ProvedorIAIndisponivel, obter_provedor
from app.services.ia.base import ContextoDivergencia
from app.services.ia.cache import chave_cache
from app.services.ia.contexto import montar_contexto_divergencia
from app.services.ia.prompt import VERSAO_PROMPT
from app.services.matching import (
    STATUS_DIVERGENTE_DATA,
    STATUS_DIVERGENTE_VALOR,
    STATUS_DUPLICADO,
    STATUS_SEM_CORRESPONDENCIA,
    STATUS_TARIFA_BANCARIA,
)

logger = logging.getLogger(__name__)

Indisponibilidade = Literal["desabilitado", "erro_provedor", "limite_diario"]

# As cinco categorias de divergência; match nunca tem explicação.
STATUS_COM_EXPLICACAO = (
    STATUS_DIVERGENTE_VALOR,
    STATUS_DIVERGENTE_DATA,
    STATUS_TARIFA_BANCARIA,
    STATUS_SEM_CORRESPONDENCIA,
    STATUS_DUPLICADO,
)


def obter_provedor_dependencia() -> ProvedorDeIA | None:
    """Dependency das rotas que usam a IA; os testes a trocam por um provedor
    falso em `app.dependency_overrides`."""
    return obter_provedor()


class LinhaNaoEncontrada(Exception):
    """A linha não existe ou é de outra empresa."""


@dataclass(frozen=True)
class Preparo:
    empresa_id: uuid.UUID
    conciliacao_id: uuid.UUID
    status: str
    contexto: ContextoDivergencia
    chave: str | None = None
    texto_cache: str | None = None


@dataclass(frozen=True)
class ResultadoGeracao:
    status: str
    explicacao: str
    gerada_por_ia: bool
    em_cache: bool
    indisponibilidade: Indisponibilidade | None


def _inicio_do_dia_utc() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def preparar(
    db: Session, empresa_id: uuid.UUID, conciliacao_id: uuid.UUID, *, com_cache: bool
) -> Preparo:
    """Lê a linha e monta o contexto (e, `com_cache`, a chave e o texto do
    cache). `LinhaNaoEncontrada` sem a linha; `ValueError` para match."""
    conciliacao = db.execute(
        select(Conciliacao).where(
            Conciliacao.id == conciliacao_id, Conciliacao.empresa_id == empresa_id
        )
    ).scalar_one_or_none()
    if conciliacao is None:
        raise LinhaNaoEncontrada
    status = conciliacao.status
    contexto = montar_contexto_divergencia(db, conciliacao)
    if not com_cache:
        return Preparo(empresa_id, conciliacao_id, status, contexto)
    chave = chave_cache(
        empresa_id,
        settings.llm_provedor,
        settings.openai_modelo,
        contexto,
        modo=settings.llm_modo_descricao,
    )
    texto_cache = db.scalar(
        select(ExplicacaoDivergencia.texto).where(
            ExplicacaoDivergencia.empresa_id == empresa_id,
            ExplicacaoDivergencia.chave == chave,
        )
    )
    return Preparo(empresa_id, conciliacao_id, status, contexto, chave, texto_cache)


def _fallback(preparo: Preparo, indisponibilidade: Indisponibilidade) -> ResultadoGeracao:
    return ResultadoGeracao(
        status=preparo.status,
        explicacao=preparo.contexto.motivo,
        gerada_por_ia=False,
        em_cache=False,
        indisponibilidade=indisponibilidade,
    )


def ligar(db: Session, empresa_id: uuid.UUID, conciliacao_id: uuid.UUID, chave: str) -> None:
    """Liga a linha ao texto do cache, sem commit. Linha que sumiu: nada."""
    db.execute(
        update(Conciliacao)
        .where(Conciliacao.id == conciliacao_id, Conciliacao.empresa_id == empresa_id)
        .values(explicacao_chave=chave)
    )


def usar_cache(db: Session, preparo: Preparo, origem: str = "explicacoes") -> ResultadoGeracao:
    """O texto do cache, ligado à linha."""
    try:
        ligar(db, preparo.empresa_id, preparo.conciliacao_id, preparo.chave)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - a ligação nunca impede a resposta
        db.rollback()
        logger.info("%s ligacao_falhou classe=%s", origem, type(exc).__name__)
    return ResultadoGeracao(
        status=preparo.status,
        explicacao=preparo.texto_cache,
        gerada_por_ia=True,
        em_cache=True,
        indisponibilidade=None,
    )


def _limite_atingido(db: Session, empresa_id: uuid.UUID) -> bool:
    hoje = _inicio_do_dia_utc()
    conta = select(Empresa.conta_id).where(Empresa.id == empresa_id).scalar_subquery()
    empresas_da_conta = select(Empresa.id).where(Empresa.conta_id == conta)
    contagem_conta = db.scalar(
        select(func.count())
        .select_from(ExplicacaoDivergencia)
        .where(
            ExplicacaoDivergencia.empresa_id.in_(empresas_da_conta),
            ExplicacaoDivergencia.criado_em >= hoje,
        )
    )
    contagem_global = db.scalar(
        select(func.count())
        .select_from(ExplicacaoDivergencia)
        .where(ExplicacaoDivergencia.criado_em >= hoje)
    )
    return (
        contagem_conta >= settings.llm_limite_diario_conta
        or contagem_global >= settings.llm_limite_diario_global
    )


def gerar(
    db: Session, preparo: Preparo, provedor: ProvedorDeIA, origem: str = "explicacoes"
) -> ResultadoGeracao:
    """Passos 3 a 6 do docstring do módulo, para uma linha sem cache."""
    if _limite_atingido(db, preparo.empresa_id):
        db.rollback()
        return _fallback(preparo, "limite_diario")

    # Fim das leituras: nenhuma transação aberta durante a chamada externa.
    db.rollback()

    identificador_anonimo = hashlib.sha256(str(preparo.empresa_id).encode()).hexdigest()
    try:
        resposta = provedor.explicar_divergencia(
            preparo.contexto, identificador_anonimo=identificador_anonimo
        )
    except ProvedorIAIndisponivel as exc:
        # Cota esgotada é o teto de gasto da OpenAI: em WARNING, para aparecer
        # no log do Railway. Para o front é um erro_provedor como outro qualquer.
        nivel = logging.WARNING if exc.motivo == "cota_esgotada" else logging.INFO
        logger.log(nivel, "%s motivo=%s status_http=%s", origem, exc.motivo, exc.status_http)
        return _fallback(preparo, "erro_provedor")
    except Exception as exc:  # noqa: BLE001 - qualquer falha do provedor vira fallback
        logger.info("%s erro_inesperado classe=%s", origem, type(exc).__name__)
        return _fallback(preparo, "erro_provedor")

    try:
        db.execute(
            pg_insert(ExplicacaoDivergencia.__table__)
            .values(
                id=uuid.uuid4(),
                empresa_id=preparo.empresa_id,
                chave=preparo.chave,
                status=preparo.status,
                provedor=settings.llm_provedor,
                modelo=settings.openai_modelo,
                versao_prompt=VERSAO_PROMPT,
                texto=resposta.texto,
                tokens_entrada=resposta.tokens_entrada,
                tokens_saida=resposta.tokens_saida,
            )
            .on_conflict_do_nothing(index_elements=["empresa_id", "chave"])
        )
        ligar(db, preparo.empresa_id, preparo.conciliacao_id, preparo.chave)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - grava o que puder, nunca perde a explicação
        db.rollback()
        logger.info("%s cache_insert_falhou classe=%s", origem, type(exc).__name__)

    return ResultadoGeracao(
        status=preparo.status,
        explicacao=resposta.texto,
        gerada_por_ia=True,
        em_cache=False,
        indisponibilidade=None,
    )


def explicar_linha(
    db: Session,
    empresa_id: uuid.UUID,
    conciliacao_id: uuid.UUID,
    provedor: ProvedorDeIA | None,
) -> ResultadoGeracao:
    """A explicação de uma linha, como o `POST /explicacoes` entrega.
    Provedor indisponível (recurso desligado): o fallback determinístico,
    mesmo com texto em cache, porque o `LLM_HABILITADO` desligado é o
    interruptor de emergência. `LinhaNaoEncontrada` e `ValueError` sobem."""
    preparo = preparar(db, empresa_id, conciliacao_id, com_cache=provedor is not None)
    if provedor is None:
        db.rollback()
        return _fallback(preparo, "desabilitado")
    if preparo.texto_cache is not None:
        return usar_cache(db, preparo)
    return gerar(db, preparo, provedor)


# --- geração automática ao conciliar ------------------------------------------------


@dataclass
class ResumoAutomatico:
    geradas: int = 0
    cache: int = 0
    teto: int = 0
    limite: int = 0
    falhas: int = 0
    linhas_por_chave: dict[str, list[Preparo]] = field(default_factory=dict, repr=False)


def automatica_ligada() -> bool:
    return settings.explicacao_automatica_habilitada and settings.llm_habilitado


def _linhas_em_ordem(
    db: Session, empresa_id: uuid.UUID, extrato_banco_id: uuid.UUID, extrato_sistema_id: uuid.UUID
) -> list[uuid.UUID]:
    """As linhas divergentes do par, da de maior valor absoluto para a menor,
    com desempate pela chave da linha."""
    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    linhas = db.execute(
        select(
            Conciliacao.id,
            Conciliacao.lancamento_banco_id,
            lanc_banco.valor.label("banco_valor"),
            lanc_sistema.valor.label("sistema_valor"),
            lanc_sistema.data.label("sistema_data"),
            lanc_sistema.descricao.label("sistema_descricao"),
            lanc_sistema.ocorrencia.label("sistema_ocorrencia"),
        )
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(
            Conciliacao.empresa_id == empresa_id,
            Conciliacao.extrato_banco_id == extrato_banco_id,
            Conciliacao.extrato_sistema_id == extrato_sistema_id,
            Conciliacao.status.in_(STATUS_COM_EXPLICACAO),
        )
    ).all()

    def _ordem(linha) -> tuple:
        valor = linha.banco_valor if linha.lancamento_banco_id is not None else linha.sistema_valor
        chave = chave_da_linha(
            linha.lancamento_banco_id,
            linha.sistema_valor,
            linha.sistema_data,
            linha.sistema_descricao,
            linha.sistema_ocorrencia,
        )
        return (-abs(valor), chave)

    return [linha.id for linha in sorted(linhas, key=_ordem)]


def _gerar_em_sessao_propria(
    preparo: Preparo, provedor: ProvedorDeIA, sessao: Callable[[], Session]
) -> ResultadoGeracao | None:
    """Uma linha, numa sessão só desta thread. `None` em erro inesperado."""
    try:
        with sessao() as db:
            return gerar(db, preparo, provedor, origem="explicacao_automatica")
    except Exception as exc:  # noqa: BLE001 - a falha de uma linha não derruba as outras
        logger.info("explicacao_automatica erro_inesperado classe=%s", type(exc).__name__)
        return None


def gerar_explicacoes_do_par(
    empresa_id: uuid.UUID,
    extrato_banco_id: uuid.UUID,
    extrato_sistema_id: uuid.UUID,
    provedor: ProvedorDeIA | None,
    sessao: Callable[[], Session] = SessionLocal,
) -> ResumoAutomatico:
    """As explicações das divergências do par conciliado, na rodada atual.
    Nunca relança exceção; devolve as contagens (que também saem no log)."""
    resumo = ResumoAutomatico()
    if provedor is None or not automatica_ligada():
        return resumo
    try:
        with sessao() as db:
            for conciliacao_id in _linhas_em_ordem(
                db, empresa_id, extrato_banco_id, extrato_sistema_id
            ):
                try:
                    preparo = preparar(db, empresa_id, conciliacao_id, com_cache=True)
                except LinhaNaoEncontrada:
                    continue  # o par foi reconciliado no meio: a rodada nova cuida
                except Exception as exc:  # noqa: BLE001 - só esta linha fica sem
                    db.rollback()
                    resumo.falhas += 1
                    logger.info(
                        "explicacao_automatica preparo_falhou classe=%s", type(exc).__name__
                    )
                    continue
                if preparo.texto_cache is not None:
                    usar_cache(db, preparo, origem="explicacao_automatica")
                    resumo.cache += 1
                elif preparo.chave in resumo.linhas_por_chave:
                    resumo.linhas_por_chave[preparo.chave].append(preparo)
                elif len(resumo.linhas_por_chave) < settings.llm_teto_por_conciliacao:
                    resumo.linhas_por_chave[preparo.chave] = [preparo]
                else:
                    resumo.teto += 1
            db.rollback()

        grupos = list(resumo.linhas_por_chave.values())
        with ThreadPoolExecutor(max_workers=settings.llm_concorrencia_automatica) as pool:
            resultados = list(
                pool.map(lambda grupo: _gerar_em_sessao_propria(grupo[0], provedor, sessao), grupos)
            )

        for grupo, resultado in zip(grupos, resultados, strict=True):
            if resultado is None or resultado.indisponibilidade == "erro_provedor":
                resumo.falhas += len(grupo)
            elif resultado.indisponibilidade == "limite_diario":
                resumo.limite += len(grupo)
            else:
                resumo.geradas += 1
                resumo.cache += len(grupo) - 1
                if len(grupo) > 1:
                    _ligar_repetidas(grupo[1:], sessao)
    except Exception as exc:  # noqa: BLE001 - a tarefa nunca derruba nada
        logger.info("explicacao_automatica erro_inesperado classe=%s", type(exc).__name__)
    finally:
        logger.info(
            "explicacao_automatica geradas=%d cache=%d teto=%d limite=%d falhas=%d",
            resumo.geradas,
            resumo.cache,
            resumo.teto,
            resumo.limite,
            resumo.falhas,
        )
    return resumo


def _ligar_repetidas(preparos: list[Preparo], sessao: Callable[[], Session]) -> None:
    """Liga as outras linhas com o mesmo contexto ao texto que acabou de ser
    gerado para a primeira."""
    try:
        with sessao() as db:
            for preparo in preparos:
                ligar(db, preparo.empresa_id, preparo.conciliacao_id, preparo.chave)
            db.commit()
    except Exception as exc:  # noqa: BLE001 - só a ligação fica de fora
        logger.info("explicacao_automatica ligacao_falhou classe=%s", type(exc).__name__)
