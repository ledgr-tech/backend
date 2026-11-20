"""Empresas clientes da conta (issue #133, ADR-019).

Rotas da conta: ignoram o cabeçalho `X-Ledgr-Empresa`. São elas que dizem
quais empresas o front pode mandar nele.

`GET /empresas` lista as empresas da conta, para todos os papéis, paginada
desde a issue #157, com o envelope das outras listagens (`total`, `limit`,
`offset`, `itens`). Parâmetros:

- `busca`: pela razão social, sem diferenciar maiúsculas nem acento, ou pelo
  CNPJ, com ou sem máscara. O CNPJ só entra na busca quando o texto, sem a
  máscara, tem algum dígito: senão "Padaria" acharia um CNPJ alfanumérico com
  essas letras. A comparação sem acento é um `translate` dos acentos do
  português (`app/core/sem_acento.py`), sem a extensão `unaccent`;
- `ordem`: `nome` (padrão), `cnpj` ou `status`, e `direcao`: `asc` (padrão) ou
  `desc`. As favoritas do usuário vêm sempre primeiro, em qualquer ordem; a
  pedida vale dentro de cada grupo. Desempate pela razão social e pelo id;
- `limit` de 1 a 100 (padrão 20) e `offset` a partir de 0. `offset` além do
  total dá 200 com a lista vazia e o total, como nas outras listagens.
  `ordem` e `direcao` fora da lista, `limit` e `offset` fora do intervalo dão
  422.

Cada item traz `favorita` e `status`, a situação do mês mais recente da
empresa. O mês é a competência (mês de `periodo_inicio`) mais recente entre
os extratos do banco que já entraram numa conciliação, a regra do fechamento
(app/services/fechamentos.py): `fechado` quando esse mês tem fechamento
ativo, `em_aberto` quando não, e `sem_conciliacao` quando a empresa nunca
conciliou (ou só extratos sem período). Tudo numa consulta por página: o
status sai de uma subconsulta agrupada por empresa, o favorito de um outer
join e o total de `count(*) OVER ()`. A página além do total não traz linha
de onde ler o total, e só ela faz uma segunda consulta, a contagem. Sem índice novo para a busca: a
consulta parte do índice de `conta_id`, e uma conta tem poucas empresas.

`PUT` e `DELETE /empresas/{empresa_id}/favorita` (issue #157) favoritam e
desfavoritam para o usuário do token, não para a conta. Idempotentes, 204,
qualquer papel. Empresa de outra conta ou inexistente dá 404. Limite de
`LIMITE_FAVORITAS_POR_USUARIO` por usuário.

`POST /empresas` cadastra uma empresa cliente. Só o admin e o gestor
(`exigir_papel`): o analista recebe 403. O CNPJ tem a mesma validação e
normalização do `/register`, alfanumérico inclusive. Regras, nesta ordem:

- 429 com `Retry-After` depois de `LIMITE_EMPRESAS_POR_HORA` empresas criadas
  pela conta na última hora, contadas na tabela (a primeira, do cadastro,
  inclusive, e as removidas também: remover não devolve a cota). É uma trava contra abuso, separada do plano, e vale mesmo sem
  limite de plano configurado. A linha da conta é lida com `FOR UPDATE`, o
  mesmo desenho do limite de convites por hora da #69;
- 409 "Empresa já cadastrada nesta conta." quando o CNPJ já é de uma empresa
  da conta. O mesmo CNPJ em outra conta é permitido; a corrida entre dois
  cadastros iguais cai no `UNIQUE (conta_id, cnpj)` e também vira 409;
- 402 no limite de empresas do plano (app/services/plano.py), para todos os
  papéis;
- consulta na Receita da #119, com as regras do cadastro: "nao_encontrado" e
  "inativa" dão 422 com `loc` `["body", "cnpj"]`; "ativa" preenche
  `cnpj_verificado_em`; desligada ou indisponível, a empresa nasce não
  verificada.

A consulta à Receita (até 5 s) roda com a transação encerrada, sem a trava da
conta. Por isso as três primeiras regras são conferidas de novo, com a trava,
depois dela. A empresa nasce com a `Configuracao` padrão.

Editar e remover (issue #215). A empresa removida some de toda consulta pelo
filtro de sessão de app/models/empresa.py (listagem, `GET /me`, limite do
plano, favoritas, busca, assistente, `/v1`, MCP, links públicos e o
`X-Ledgr-Empresa`, que dá 404); nenhuma rota repete a regra.

- `PATCH /empresas/{id}` muda a `razao_social` (aparada, não vazia). Admin e
  gestor; analista 403; empresa de outra conta ou removida 404. O corpo recusa
  campo desconhecido (422) e aceita só o que veio, para a #236 acrescentar o
  nome de exibição sem quebrar. O CNPJ não muda. Na empresa principal (o mesmo
  CNPJ da conta) a razão social da conta muda junto, na mesma transação;
- `DELETE /empresas/{id}` remove: admin e gestor. A empresa sai na hora, as
  conexões MCP dela são revogadas e os dados são apagados 30 dias depois
  (`apagar_em`), se ninguém restaurar. Recusas, em 409 com `detail` e `codigo`:
  `empresa_principal` (a do CNPJ da conta) e `ultima_empresa` (a última
  ativa). Responde 200 com a empresa removida (`EmpresaRemovidaResponse`). Depois
  do commit e da resposta, e-mail ao admin e a quem removeu; falha no envio não
  desfaz nada;
- `GET /empresas/removidas`: admin e gestor, as removidas que ainda dá para
  restaurar, da remoção mais recente para a mais antiga;
- `POST /empresas/{id}/restaurar`: admin e gestor, só antes de `apagar_em` (depois,
  404) e dentro do limite de empresas do plano (402). Volta tudo como estava,
  menos as conexões MCP;
- o `POST /empresas` de um CNPJ removido e ainda não apagado dá 409 com `codigo`
  `empresa_removida`, o `empresa_id` e o `apagar_em`, para o front oferecer
  restaurar; se o prazo já passou e a limpeza ainda não apagou, `codigo`
  `empresa_sendo_apagada`, sem oferta.

Logs só com o resultado categorizado; nunca o CNPJ nem a razão social.
"""

import logging
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import DateTime, and_, case, cast, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import (
    CnpjValido,
    ConsultaCnpjDependencia,
    TextoPreenchido,
    barrar_cnpj_recusado,
    cnpj_verificado_em,
    situacao_na_receita,
)
from app.core.auth import exigir_papel, obter_usuario_autenticado
from app.core.database import get_db
from app.core.rate_limit import (
    LIMITE_EMPRESA_ESCRITA_POR_USUARIO,
    LIMITE_FAVORITAS_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.core.sem_acento import sem_acento, sem_acento_sql
from app.models import (
    Configuracao,
    Conta,
    Empresa,
    EmpresaFavorita,
    ExecucaoConciliacao,
    Extrato,
    Fechamento,
    Usuario,
)
from app.models.empresa import INCLUIR_REMOVIDAS
from app.services import empresas_removidas as remocao
from app.services.email import ProvedorDeEmail
from app.services.email.envio import obter_provedor_email_dependencia
from app.services.plano import barrar_no_limite, empresas_usadas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/empresas", tags=["empresas"])

LIMITE_EMPRESAS_POR_HORA = 20

_MASCARA_CNPJ = re.compile(r"[.\-/\s]")

Ordem = Literal["nome", "cnpj", "status"]
Direcao = Literal["asc", "desc"]
StatusEmpresa = Literal["fechado", "em_aberto", "sem_conciliacao"]


class EmpresaRequest(BaseModel):
    cnpj: CnpjValido
    razao_social: TextoPreenchido


class EmpresaPatchRequest(BaseModel):
    """Só o que veio no corpo é gravado (`model_fields_set`); `null` explícito é
    422, e o corpo sem campo nenhum também. Para o campo da #236 (nome de
    exibição) bastar acrescentar uma linha aqui e outra em `editar_empresa`."""

    model_config = ConfigDict(extra="forbid")

    razao_social: TextoPreenchido = None

    @model_validator(mode="after")
    def _algum_campo(self) -> "EmpresaPatchRequest":
        if not self.model_fields_set:
            raise ValueError("Informe ao menos um campo para alterar.")
        return self


class EmpresaResponse(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str
    cnpj_verificado_em: datetime | None
    criado_em: datetime


class EmpresaRemovidaResponse(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str
    removida_em: datetime
    removida_por_nome: str
    apagar_em: datetime


class EmpresasRemovidasResponse(BaseModel):
    itens: list[EmpresaRemovidaResponse]


class ConflitoDeEmpresa(Exception):
    """409 com o `detail` em texto, que o front mostra direto, e o `codigo` ao
    lado, para ele decidir o que oferecer. `extra` entra no corpo (ex.:
    `empresa_id`)."""

    def __init__(self, detail: str, codigo: str, **extra) -> None:
        self.corpo = {"detail": detail, "codigo": codigo, **extra}
        super().__init__(detail)


async def tratar_conflito(_request: Request, exc: ConflitoDeEmpresa) -> JSONResponse:
    return JSONResponse(status_code=http_status.HTTP_409_CONFLICT, content=exc.corpo)


class EmpresaItemResponse(EmpresaResponse):
    favorita: bool
    status: StatusEmpresa


class EmpresaListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[EmpresaItemResponse]


def _resposta(empresa: Empresa) -> EmpresaResponse:
    # `criado_em` é `TIMESTAMP` sem fuso, gravado em UTC (TimestampMixin).
    return EmpresaResponse(
        id=empresa.id,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        cnpj_verificado_em=empresa.cnpj_verificado_em,
        criado_em=empresa.criado_em.replace(tzinfo=UTC),
    )


def _filtro_da_busca(busca: str | None):
    """Condição da busca, ou None sem texto (regra no docstring do módulo)."""
    texto = (busca or "").strip()
    if not texto:
        return None
    condicoes = [sem_acento_sql(Empresa.razao_social).contains(sem_acento(texto), autoescape=True)]
    cnpj = _MASCARA_CNPJ.sub("", texto).upper()
    if cnpj.isalnum() and any(caractere.isdigit() for caractere in cnpj):
        condicoes.append(Empresa.cnpj.contains(cnpj, autoescape=True))
    return or_(*condicoes)


def _status_das_empresas(conta_id: uuid.UUID):
    """`status` de cada empresa da conta como expressão, a partir da
    competência mais recente com conciliação (subconsulta agrupada, uma linha
    por empresa que já conciliou)."""
    competencias = (
        select(
            Extrato.empresa_id,
            func.max(func.to_char(Extrato.periodo_inicio, "YYYY-MM")).label("competencia"),
        )
        .join(ExecucaoConciliacao, ExecucaoConciliacao.extrato_banco_id == Extrato.id)
        .join(Empresa, Empresa.id == Extrato.empresa_id)
        .where(Empresa.conta_id == conta_id, Extrato.periodo_inicio.is_not(None))
        .group_by(Extrato.empresa_id)
        .subquery()
    )
    fechado = exists().where(
        Fechamento.empresa_id == Empresa.id,
        Fechamento.competencia == competencias.c.competencia,
        Fechamento.reaberto_em.is_(None),
    )
    status = case(
        (competencias.c.competencia.is_(None), "sem_conciliacao"),
        (fechado, "fechado"),
        else_="em_aberto",
    )
    return competencias, status


@router.get("", response_model=EmpresaListaResponse)
def listar_empresas(
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    busca: Annotated[str | None, Query(max_length=100)] = None,
    ordem: Annotated[Ordem, Query()] = "nome",
    direcao: Annotated[Direcao, Query()] = "asc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EmpresaListaResponse:
    competencias, status_expr = _status_das_empresas(usuario.conta_id)
    favorita = EmpresaFavorita.usuario_id.is_not(None).label("favorita")
    status = status_expr.label("status")
    nome = sem_acento_sql(Empresa.razao_social)
    chave = {"nome": nome, "cnpj": Empresa.cnpj, "status": status}[ordem]

    consulta = (
        select(Empresa, favorita, status, func.count().over().label("total"))
        .outerjoin(competencias, competencias.c.empresa_id == Empresa.id)
        .outerjoin(
            EmpresaFavorita,
            and_(
                EmpresaFavorita.empresa_id == Empresa.id,
                EmpresaFavorita.usuario_id == usuario.id,
            ),
        )
        .order_by(
            favorita.desc(),
            chave.desc() if direcao == "desc" else chave.asc(),
            nome,
            Empresa.id,
        )
        .limit(limit)
        .offset(offset)
    )
    filtros = [Empresa.conta_id == usuario.conta_id]
    filtro = _filtro_da_busca(busca)
    if filtro is not None:
        filtros.append(filtro)
    linhas = db.execute(consulta.where(*filtros)).all()

    if linhas:
        total = linhas[0].total
    elif offset > 0:
        # O offset passou do total: a página vazia não traz o `count(*) OVER ()`.
        total = db.scalar(select(func.count()).select_from(Empresa).where(*filtros))
    else:
        total = 0
    return EmpresaListaResponse(
        total=total,
        limit=limit,
        offset=offset,
        itens=[
            EmpresaItemResponse(
                **_resposta(linha.Empresa).model_dump(),
                favorita=linha.favorita,
                status=linha.status,
            )
            for linha in linhas
        ],
    )


def _empresa_da_conta(db: Session, empresa_id: uuid.UUID, conta_id: uuid.UUID) -> Empresa:
    """A empresa, ou 404 se não existe ou é de outra conta (sem confirmar qual)."""
    empresa = db.scalar(
        select(Empresa).where(Empresa.id == empresa_id, Empresa.conta_id == conta_id)
    )
    if empresa is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    return empresa


@router.put("/{empresa_id}/favorita", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_FAVORITAS_POR_USUARIO, key_func=chave_por_usuario)
def favoritar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    empresa = _empresa_da_conta(db, empresa_id, usuario.conta_id)
    # Favoritar de novo não muda nada, nem o `criado_em`.
    db.execute(
        insert(EmpresaFavorita)
        .values(usuario_id=usuario.id, empresa_id=empresa.id, conta_id=usuario.conta_id)
        .on_conflict_do_nothing(index_elements=["usuario_id", "empresa_id"])
    )
    db.commit()


@router.delete("/{empresa_id}/favorita", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_FAVORITAS_POR_USUARIO, key_func=chave_por_usuario)
def desfavoritar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    empresa = _empresa_da_conta(db, empresa_id, usuario.conta_id)
    db.execute(
        delete(EmpresaFavorita).where(
            EmpresaFavorita.usuario_id == usuario.id, EmpresaFavorita.empresa_id == empresa.id
        )
    )
    db.commit()


def _conferir_com_a_conta_travada(db: Session, conta_id: uuid.UUID, cnpj: str) -> Conta:
    """Trava a linha da conta e aplica, nesta ordem, o 429 por hora, o 409 do
    CNPJ repetido na conta e o 402 do plano. Devolve a conta travada."""
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    # Tudo no relógio do Postgres, que é quem grava `criado_em`. O Retry-After
    # é o tempo até a empresa mais antiga da janela sair dela.
    criadas, segundos_ate_liberar = db.execute(
        select(
            func.count(),
            func.extract(
                "epoch",
                func.min(cast(Empresa.criado_em, DateTime(timezone=True)))
                + timedelta(hours=1)
                - func.now(),
            ),
        ).where(
            Empresa.conta_id == conta_id,
            Empresa.criado_em >= func.now() - timedelta(hours=1),
        )
        # Com as removidas (issue #215): senão cadastrar, remover e cadastrar de
        # novo furaria a trava contra abuso.
        .execution_options(**{INCLUIR_REMOVIDAS: True})
    ).one()
    if criadas >= LIMITE_EMPRESAS_POR_HORA:
        logger.info("cadastro_empresa resultado=limite_por_hora")
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de empresas cadastradas por hora atingido. Tente novamente mais tarde.",
            headers={"Retry-After": str(max(1, math.ceil(segundos_ate_liberar)))},
        )

    existente = db.scalar(
        select(Empresa)
        .where(Empresa.conta_id == conta_id, Empresa.cnpj == cnpj)
        .execution_options(**{INCLUIR_REMOVIDAS: True})
    )
    if existente is not None:
        _barrar_cnpj_ja_na_conta(existente)

    try:
        barrar_no_limite(conta, "empresas", empresas_usadas(db, conta_id))
    except HTTPException:
        logger.info("cadastro_empresa resultado=limite_do_plano")
        raise
    return conta


def _barrar_cnpj_ja_na_conta(existente: Empresa) -> None:
    """409 do CNPJ que já é de uma empresa da conta. Se ela está removida e
    ainda não foi apagada, o `codigo` diz isso para o front oferecer restaurar
    (issue #215); a `UNIQUE (conta_id, cnpj)` continua valendo."""
    if existente.removida_em is None:
        logger.info("cadastro_empresa resultado=cnpj_na_conta")
        raise _ja_cadastrada()
    if remocao.venceu(existente):
        logger.info("cadastro_empresa resultado=cnpj_sendo_apagado")
        raise ConflitoDeEmpresa(
            "Esta empresa foi removida e está sendo apagada. Tente cadastrá-la de novo mais tarde.",
            "empresa_sendo_apagada",
        )
    logger.info("cadastro_empresa resultado=cnpj_removido")
    raise ConflitoDeEmpresa(
        "Esta empresa foi removida da conta e ainda pode ser restaurada.",
        "empresa_removida",
        empresa_id=str(existente.id),
        apagar_em=existente.apagar_em.isoformat(),
    )


def _ja_cadastrada() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="Empresa já cadastrada nesta conta."
    )


@router.post("", response_model=EmpresaResponse, status_code=http_status.HTTP_201_CREATED)
def cadastrar_empresa(
    dados: EmpresaRequest,
    usuario: Annotated[Usuario, Depends(exigir_papel("admin", "gestor"))],
    db: Annotated[Session, Depends(get_db)],
    consulta: ConsultaCnpjDependencia,
) -> EmpresaResponse:
    conta_id = usuario.conta_id
    _conferir_com_a_conta_travada(db, conta_id, dados.cnpj)

    # Encerra a transação (e a trava) antes da chamada externa.
    receita = situacao_na_receita(db, consulta, dados.cnpj)
    if receita.situacao in ("nao_encontrado", "inativa"):
        logger.info("cadastro_empresa resultado=cnpj_%s", receita.situacao)
    barrar_cnpj_recusado(receita, dados.cnpj)

    _conferir_com_a_conta_travada(db, conta_id, dados.cnpj)
    empresa = Empresa(
        id=uuid.uuid4(),
        conta_id=conta_id,
        razao_social=dados.razao_social,
        cnpj=dados.cnpj,
        cnpj_verificado_em=cnpj_verificado_em(receita),
    )
    db.add_all([empresa, Configuracao(empresa_id=empresa.id)])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.info("cadastro_empresa resultado=cnpj_na_conta")
        raise _ja_cadastrada() from exc
    db.refresh(empresa)
    logger.info("cadastro_empresa resultado=cadastrada")
    return _resposta(empresa)


# --- editar, remover e restaurar (issue #215) -----------------------------------------

obter_admin_ou_gestor = exigir_papel("admin", "gestor")


def _removida(empresa: Empresa) -> EmpresaRemovidaResponse:
    return EmpresaRemovidaResponse(
        id=empresa.id,
        razao_social=empresa.razao_social,
        cnpj=empresa.cnpj,
        removida_em=empresa.removida_em,
        removida_por_nome=empresa.removida_por_nome,
        apagar_em=empresa.apagar_em,
    )


def _empresa_travada(db: Session, empresa_id: uuid.UUID, conta_id: uuid.UUID) -> Empresa:
    """A empresa ativa da conta, com a linha travada, ou 404. O filtro de
    sessão já esconde a removida."""
    empresa = db.scalar(
        select(Empresa)
        .where(Empresa.id == empresa_id, Empresa.conta_id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if empresa is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    return empresa


@router.patch("/{empresa_id}", response_model=EmpresaResponse)
@limiter.limit(LIMITE_EMPRESA_ESCRITA_POR_USUARIO, key_func=chave_por_usuario)
def editar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    dados: EmpresaPatchRequest,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> EmpresaResponse:
    # A conta primeiro, depois a empresa: a ordem das travas de remover,
    # restaurar e da limpeza (outra ordem travaria uma contra a outra).
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == usuario.conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    empresa = _empresa_travada(db, empresa_id, usuario.conta_id)
    campos = dados.model_fields_set
    if "razao_social" in campos:
        empresa.razao_social = dados.razao_social
        # A empresa principal é a da conta: as duas razões sociais andam juntas.
        if remocao.eh_principal(empresa, conta):
            conta.razao_social = dados.razao_social
    db.commit()
    db.refresh(empresa)
    logger.info(
        "editar_empresa resultado=editada principal=%s", remocao.eh_principal(empresa, conta)
    )
    return _resposta(empresa)


@router.delete("/{empresa_id}", response_model=EmpresaRemovidaResponse)
@limiter.limit(LIMITE_EMPRESA_ESCRITA_POR_USUARIO, key_func=chave_por_usuario)
def remover_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    tarefas: BackgroundTasks,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> EmpresaRemovidaResponse:
    conta_id = usuario.conta_id
    # A trava da conta serializa duas remoções ao mesmo tempo: sem ela, as duas
    # contariam duas empresas ativas e levariam as duas embora.
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    empresa = _empresa_travada(db, empresa_id, conta_id)
    if remocao.eh_principal(empresa, conta):
        db.rollback()
        logger.info("remover_empresa resultado=principal")
        raise ConflitoDeEmpresa(remocao.MENSAGEM_PRINCIPAL, remocao.CODIGO_PRINCIPAL)
    if empresas_usadas(db, conta_id) <= 1:
        db.rollback()
        logger.info("remover_empresa resultado=ultima")
        raise ConflitoDeEmpresa(remocao.MENSAGEM_ULTIMA, remocao.CODIGO_ULTIMA)

    destinatarios = remocao.destinatarios_do_aviso(db, conta_id, usuario)
    feita = remocao.remover(db, empresa, usuario)
    resposta = EmpresaRemovidaResponse(
        id=feita.empresa_id,
        razao_social=feita.razao_social,
        cnpj=feita.cnpj,
        removida_em=feita.removida_em,
        removida_por_nome=feita.removida_por_nome,
        apagar_em=feita.apagar_em,
    )
    db.commit()
    logger.info("remover_empresa resultado=removida")
    # Depois do commit e da resposta: o envio nunca desfaz a remoção.
    remocao.agendar_aviso(tarefas, provedor, feita, destinatarios)
    return resposta


@router.get("/removidas", response_model=EmpresasRemovidasResponse)
def listar_removidas(
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> EmpresasRemovidasResponse:
    """As empresas que ainda dá para restaurar. As de prazo vencido esperam só a
    limpeza e não aparecem."""
    empresas = db.scalars(
        select(Empresa)
        .where(
            Empresa.conta_id == usuario.conta_id,
            Empresa.removida_em.is_not(None),
            Empresa.apagar_em > remocao.agora(),
        )
        .order_by(Empresa.removida_em.desc(), Empresa.id)
        .execution_options(**{INCLUIR_REMOVIDAS: True})
    )
    return EmpresasRemovidasResponse(itens=[_removida(empresa) for empresa in empresas])


@router.post("/{empresa_id}/restaurar", response_model=EmpresaResponse)
@limiter.limit(LIMITE_EMPRESA_ESCRITA_POR_USUARIO, key_func=chave_por_usuario)
def restaurar_empresa(
    request: Request,
    empresa_id: uuid.UUID,
    usuario: Annotated[Usuario, Depends(obter_admin_ou_gestor)],
    db: Annotated[Session, Depends(get_db)],
) -> EmpresaResponse:
    conta_id = usuario.conta_id
    conta = db.scalar(
        select(Conta)
        .where(Conta.id == conta_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    empresa = db.scalar(
        select(Empresa)
        .where(
            Empresa.id == empresa_id,
            Empresa.conta_id == conta_id,
            Empresa.removida_em.is_not(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True, **{INCLUIR_REMOVIDAS: True})
    )
    if empresa is None or remocao.venceu(empresa):
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Empresa não encontrada."
        )
    try:
        barrar_no_limite(conta, "empresas", empresas_usadas(db, conta_id))
    except HTTPException:
        db.rollback()
        logger.info("restaurar_empresa resultado=limite_do_plano")
        raise
    remocao.restaurar(empresa)
    db.commit()
    db.refresh(empresa)
    logger.info("restaurar_empresa resultado=restaurada")
    return _resposta(empresa)
