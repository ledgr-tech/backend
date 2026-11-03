"""Painel administrativo, parte 2: pagamentos, cancelamentos, vencimentos e cupom
(issue #169, ADR-025 e ADR-026).

Mesmo prefixo `/admin` e mesma guarda da parte 1 (app/api/admin.py): só o
operador (403 para os outros, inclusive o admin de conta), login recente de 2
horas (401), acesso registrado em `acessos_admin`, limite por usuário
(`LIMITE_ADMIN_LEITURA_POR_USUARIO` nas consultas, `LIMITE_ADMIN_ACAO_POR_USUARIO`
nas ações). Ignora `X-Ledgr-Empresa`.

Consultas, sem chamar a Stripe: leem o que o webhook guardou em
`assinaturas_stripe` e `pagamentos_stripe`.
- `GET /admin/contas/{conta_id}/pagamentos`: o histórico da conta, paginado
  (`limit`, `offset`), do mais recente ao mais antigo.
- `GET /admin/pagamentos`: a lista global, por `status` (`paga`, `falhou`) e
  período (`desde`, `ate`, datas inclusivas no fuso de São Paulo), paginada; o
  `status=falhou` é a inadimplência.
- `GET /admin/cancelamentos`: assinaturas canceladas (`cancelada`) e com
  cancelamento agendado (`agendada`), com motivo, origem, data e plano; filtros
  `situacao`, `origem`, `desde` e `ate` (sobre `data`, o instante do pedido de
  cancelamento), paginada, da mais recente à mais antiga.
- `GET /admin/vencimentos?dias=7` (1 a 60): as assinaturas `active` ou `trialing`
  cujo `periodo_atual_fim` cai nos próximos N dias (marcando as de cancelamento
  agendado) e as contas em teste (sem assinatura) que terminam nesse prazo, numa
  lista só, da data mais próxima à mais distante.

Ações, que chamam a Stripe (app/services/assinatura/operador.py): cancelar
(`fim_do_periodo` ou `agora`, com motivo; sem reembolso nem proporcional),
manter (desfaz o cancelamento agendado), listar os cupons da Stripe e dar um
cupom à conta. Cada uma grava um evento em `eventos_conta` e sincroniza a
assinatura na hora. A cobrança desligada dá 503; a Stripe fora do ar, 502.

Logs só com ids e o resultado, nunca nome, e-mail, CNPJ, código do cupom nem
termo buscado. Nunca dado de lançamento nem das empresas clientes.
"""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.admin import (
    ContaDetalheResponse,
    MotivoRequest,
    OperadorAutenticado,
    PagamentoResponse,
    _conta,
    _detalhe,
    _evento,
    _pagamento,
    _situacao,
    obter_operador,
)
from app.core import plano
from app.core.database import get_db
from app.core.frontend_url import frontend_url_para_link
from app.core.rate_limit import (
    LIMITE_ADMIN_ACAO_POR_USUARIO,
    LIMITE_ADMIN_LEITURA_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import AssinaturaStripe, Conta, PagamentoStripe, Usuario
from app.services.assinatura import cupons, operador, stripe_api
from app.services.email import ProvedorDeEmail
from app.services.email.envio import obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_cupom
from app.services.plano import FUSO_DA_FRANQUIA

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

STATUS_ANDANDO = ("active", "trialing")
DIAS_DE_VENCIMENTO_PADRAO = 7
PERIODO_INVERTIDO = "A data inicial é depois da final."

StatusDoPagamento = Literal["paga", "falhou"]
OrigemDoCancelamento = Literal["cliente", "falta_de_pagamento", "operador", "painel_stripe"]
SituacaoDoCancelamento = Literal["cancelada", "agendada"]


# --- respostas ------------------------------------------------------------------------


class PagamentosDaContaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[PagamentoResponse]


class PagamentoDaListaResponse(PagamentoResponse):
    conta_id: uuid.UUID
    razao_social: str


class PagamentosListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[PagamentoDaListaResponse]


class CancelamentoResponse(BaseModel):
    conta_id: uuid.UUID
    razao_social: str
    situacao: SituacaoDoCancelamento
    plano: str
    periodo: str
    status: str
    data: datetime
    periodo_atual_fim: datetime | None
    motivo_cancelamento: str | None
    origem_cancelamento: str | None


class CancelamentosListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[CancelamentoResponse]


class VencimentoResponse(BaseModel):
    tipo: Literal["assinatura", "teste"]
    conta_id: uuid.UUID
    razao_social: str
    plano: str | None
    periodo: str | None
    status: str | None
    vence_em: datetime
    cancelamento_agendado: bool
    inativa: bool


class VencimentosResponse(BaseModel):
    dias: int
    total: int
    limit: int
    offset: int
    itens: list[VencimentoResponse]


class CupomDaStripeResponse(BaseModel):
    id: str
    nome: str
    percent_off: float | None
    amount_off: int | None
    moeda: str | None
    duration: str
    duration_in_months: int | None
    resumo: str


class CancelarRequest(MotivoRequest):
    quando: Literal["fim_do_periodo", "agora"]


class DarCupomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cupom_id: Annotated[str, Field(min_length=1, max_length=200)]
    validade_dias: Annotated[int, Field(ge=1, le=90)] = 30


class CupomDadoResponse(BaseModel):
    """O cupom dado, com o código (só na forma "codigo") para o operador copiar
    e mandar por fora se o e-mail não saiu."""

    id: uuid.UUID
    cupom: str
    desconto: str
    forma: Literal["codigo", "assinatura"]
    codigo: str | None
    expira_em: datetime | None
    email_enviado: bool


# --- consultas ------------------------------------------------------------------------


def _periodo(desde: date | None, ate: date | None) -> tuple[datetime | None, datetime | None]:
    """`[inicio, fim)` em UTC: `desde` a partir da meia-noite de São Paulo e
    `ate` até o fim do dia dele."""
    if desde is not None and ate is not None and desde > ate:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=PERIODO_INVERTIDO
        )
    inicio = (
        datetime.combine(desde, time.min, tzinfo=FUSO_DA_FRANQUIA).astimezone(UTC)
        if desde
        else None
    )
    fim = (
        datetime.combine(ate + timedelta(days=1), time.min, tzinfo=FUSO_DA_FRANQUIA).astimezone(UTC)
        if ate
        else None
    )
    return inicio, fim


@router.get("/contas/{conta_id}/pagamentos", response_model=PagamentosDaContaResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_pagamentos_da_conta(
    request: Request,
    conta_id: uuid.UUID,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PagamentosDaContaResponse:
    _conta(db, conta_id)
    do_conta = PagamentoStripe.conta_id == conta_id
    pagamentos = db.scalars(
        select(PagamentoStripe)
        .where(do_conta)
        .order_by(PagamentoStripe.ocorrido_em.desc(), PagamentoStripe.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return PagamentosDaContaResponse(
        total=db.scalar(select(func.count()).select_from(PagamentoStripe).where(do_conta)),
        limit=limit,
        offset=offset,
        itens=[_pagamento(pagamento) for pagamento in pagamentos],
    )


@router.get("/pagamentos", response_model=PagamentosListaResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_pagamentos(
    request: Request,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[StatusDoPagamento | None, Query()] = None,
    desde: Annotated[date | None, Query()] = None,
    ate: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PagamentosListaResponse:
    inicio, fim = _periodo(desde, ate)
    filtros = []
    if status is not None:
        filtros.append(PagamentoStripe.status == status)
    if inicio is not None:
        filtros.append(PagamentoStripe.ocorrido_em >= inicio)
    if fim is not None:
        filtros.append(PagamentoStripe.ocorrido_em < fim)
    base = select(PagamentoStripe, Conta.razao_social).join(
        Conta, Conta.id == PagamentoStripe.conta_id
    )
    total = db.scalar(select(func.count()).select_from(PagamentoStripe).where(*filtros))
    linhas = db.execute(
        base.where(*filtros)
        .order_by(PagamentoStripe.ocorrido_em.desc(), PagamentoStripe.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return PagamentosListaResponse(
        total=total,
        limit=limit,
        offset=offset,
        itens=[
            PagamentoDaListaResponse(
                **_pagamento(linha.PagamentoStripe).model_dump(),
                conta_id=linha.PagamentoStripe.conta_id,
                razao_social=linha.razao_social,
            )
            for linha in linhas
        ],
    )


@router.get("/cancelamentos", response_model=CancelamentosListaResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_cancelamentos(
    request: Request,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    situacao: Annotated[SituacaoDoCancelamento | None, Query()] = None,
    origem: Annotated[OrigemDoCancelamento | None, Query()] = None,
    desde: Annotated[date | None, Query()] = None,
    ate: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CancelamentosListaResponse:
    inicio, fim = _periodo(desde, ate)
    cancelada = AssinaturaStripe.status == "canceled"
    agendada = AssinaturaStripe.cancelar_no_fim_do_periodo & AssinaturaStripe.status.in_(
        operador.STATUS_VIVOS
    )
    # `cancelada_em` é o instante do pedido (a Stripe o grava já ao agendar).
    data = func.coalesce(AssinaturaStripe.cancelada_em, AssinaturaStripe.atualizada_em)
    filtros = [cancelada | agendada]
    if situacao == "cancelada":
        filtros.append(cancelada)
    elif situacao == "agendada":
        filtros.append(~cancelada & agendada)
    if origem is not None:
        filtros.append(AssinaturaStripe.origem_cancelamento == origem)
    if inicio is not None:
        filtros.append(data >= inicio)
    if fim is not None:
        filtros.append(data < fim)
    linhas = db.execute(
        select(
            AssinaturaStripe,
            Conta.razao_social,
            data.label("data"),
        )
        .join(Conta, Conta.id == AssinaturaStripe.conta_id)
        .where(*filtros)
        .order_by(data.desc(), AssinaturaStripe.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return CancelamentosListaResponse(
        total=db.scalar(select(func.count()).select_from(AssinaturaStripe).where(*filtros)),
        limit=limit,
        offset=offset,
        itens=[
            CancelamentoResponse(
                conta_id=linha.AssinaturaStripe.conta_id,
                razao_social=linha.razao_social,
                situacao="cancelada" if linha.AssinaturaStripe.status == "canceled" else "agendada",
                plano=linha.AssinaturaStripe.plano,
                periodo=linha.AssinaturaStripe.periodo,
                status=linha.AssinaturaStripe.status,
                data=linha.data,
                periodo_atual_fim=linha.AssinaturaStripe.periodo_atual_fim,
                motivo_cancelamento=linha.AssinaturaStripe.motivo_cancelamento,
                origem_cancelamento=linha.AssinaturaStripe.origem_cancelamento,
            )
            for linha in linhas
        ],
    )


@router.get("/vencimentos", response_model=VencimentosResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_vencimentos(
    request: Request,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    dias: Annotated[int, Query(ge=1, le=60)] = DIAS_DE_VENCIMENTO_PADRAO,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VencimentosResponse:
    agora = plano.agora()
    limite = agora + timedelta(days=dias)
    assinaturas = (
        AssinaturaStripe.status.in_(STATUS_ANDANDO),
        AssinaturaStripe.periodo_atual_fim >= agora,
        AssinaturaStripe.periodo_atual_fim <= limite,
    )
    situacao = _situacao(agora)
    testes = (
        situacao == "teste",
        Conta.teste_termina_em >= agora,
        Conta.teste_termina_em <= limite,
    )
    # Cada fonte já vem ordenada; só as primeiras `offset + limit` de cada uma
    # podem entrar na página da lista junta.
    ate_aqui = offset + limit
    da_assinatura = db.execute(
        select(AssinaturaStripe, Conta)
        .join(Conta, Conta.id == AssinaturaStripe.conta_id)
        .where(*assinaturas)
        .order_by(AssinaturaStripe.periodo_atual_fim, AssinaturaStripe.id)
        .limit(ate_aqui)
    ).all()
    do_teste = db.execute(
        select(Conta).where(*testes).order_by(Conta.teste_termina_em, Conta.id).limit(ate_aqui)
    ).all()
    total = db.scalar(
        select(func.count())
        .select_from(AssinaturaStripe)
        .join(Conta, Conta.id == AssinaturaStripe.conta_id)
        .where(*assinaturas)
    ) + db.scalar(select(func.count()).select_from(Conta).where(*testes))
    itens = [
        VencimentoResponse(
            tipo="assinatura",
            conta_id=conta.id,
            razao_social=conta.razao_social,
            plano=assinatura.plano,
            periodo=assinatura.periodo,
            status=assinatura.status,
            vence_em=assinatura.periodo_atual_fim,
            cancelamento_agendado=assinatura.cancelar_no_fim_do_periodo,
            inativa=conta.inativada_em is not None,
        )
        for assinatura, conta in da_assinatura
    ] + [
        VencimentoResponse(
            tipo="teste",
            conta_id=conta.id,
            razao_social=conta.razao_social,
            plano=conta.plano,
            periodo=None,
            status=None,
            vence_em=conta.teste_termina_em,
            cancelamento_agendado=False,
            inativa=conta.inativada_em is not None,
        )
        for (conta,) in do_teste
    ]
    itens.sort(key=lambda item: (item.vence_em, str(item.conta_id)))
    return VencimentosResponse(
        dias=dias, total=total, limit=limit, offset=offset, itens=itens[offset:ate_aqui]
    )


@router.get("/cupons", response_model=list[CupomDaStripeResponse])
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_cupons(
    request: Request,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
) -> list[dict]:
    return cupons.cupons_validos()


# --- ações ----------------------------------------------------------------------------


def _erro_da_stripe(exc: stripe.StripeError, onde: str) -> HTTPException:
    return stripe_api.erro_da_stripe(exc, onde)


@router.post("/contas/{conta_id}/assinatura/cancelar", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def cancelar_assinatura(
    request: Request,
    conta_id: uuid.UUID,
    corpo: CancelarRequest,
    quem: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    chave = stripe_api.exigir_stripe()
    conta = _conta(db, conta_id, travar=True)
    try:
        tipo = operador.cancelar_assinatura(db, chave, conta, corpo.quando)
    except stripe.StripeError as exc:
        db.rollback()
        raise _erro_da_stripe(exc, "admin_cancelar") from exc
    _evento(db, conta, tipo, corpo.motivo, quem)
    db.commit()
    db.refresh(conta)
    logger.info("admin_assinatura acao=%s conta_id=%s", tipo, conta.id)
    return _detalhe(db, conta)


@router.post("/contas/{conta_id}/assinatura/manter", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def manter_assinatura(
    request: Request,
    conta_id: uuid.UUID,
    corpo: MotivoRequest,
    quem: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    chave = stripe_api.exigir_stripe()
    conta = _conta(db, conta_id, travar=True)
    try:
        operador.manter_assinatura(db, chave, conta)
    except stripe.StripeError as exc:
        db.rollback()
        raise _erro_da_stripe(exc, "admin_manter") from exc
    _evento(db, conta, "cancelamento_desfeito", corpo.motivo, quem)
    db.commit()
    db.refresh(conta)
    logger.info("admin_assinatura acao=cancelamento_desfeito conta_id=%s", conta.id)
    return _detalhe(db, conta)


def _admin_da_conta(db: Session, conta_id: uuid.UUID) -> Usuario | None:
    return db.scalars(
        select(Usuario)
        .where(
            Usuario.conta_id == conta_id,
            Usuario.papel == "admin",
            Usuario.removido_em.is_(None),
        )
        .limit(1)
    ).first()


@router.post("/contas/{conta_id}/cupom", response_model=CupomDadoResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def dar_cupom(
    request: Request,
    conta_id: uuid.UUID,
    corpo: DarCupomRequest,
    quem: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> CupomDadoResponse:
    chave = stripe_api.exigir_stripe()
    conta = _conta(db, conta_id)
    admin = _admin_da_conta(db, conta_id)
    if admin is None:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=operador.SEM_ADMIN)
    nome, email = admin.nome, admin.email
    try:
        registro = operador.dar_cupom(
            db,
            chave,
            conta,
            admin_email=email,
            cupom_id=corpo.cupom_id,
            validade_dias=corpo.validade_dias,
            operador_id=quem.operador.id,
            operador_nome=quem.usuario.nome,
        )
    except stripe.StripeError as exc:
        db.rollback()
        raise _erro_da_stripe(exc, "admin_cupom") from exc
    db.flush()
    resumo = f"Cupom {registro.cupom_nome} ({registro.desconto}), forma {registro.forma}"
    _evento(db, conta, "cupom_enviado", resumo[:500], quem)
    db.commit()

    base = frontend_url_para_link()
    mensagem = mensagem_cupom(
        para=email,
        nome=nome,
        razao_social=conta.razao_social,
        desconto=registro.desconto,
        codigo=registro.codigo,
        validade=(
            registro.expira_em.astimezone(FUSO_DA_FRANQUIA).strftime("%d/%m/%Y")
            if registro.expira_em
            else None
        ),
        link_assinatura=f"{base}/assinatura" if base else None,
        chave_idempotencia=f"cupom-{registro.id}",
    )
    registro.email_enviado = operador.enviar_email_do_cupom(provedor, mensagem)
    db.commit()
    logger.info(
        "admin_cupom acao=enviado conta_id=%s forma=%s email_enviado=%s",
        conta.id,
        registro.forma,
        registro.email_enviado,
    )
    return CupomDadoResponse(
        id=registro.id,
        cupom=registro.cupom_nome,
        desconto=registro.desconto,
        forma=registro.forma,
        codigo=registro.codigo,
        expira_em=registro.expira_em,
        email_enviado=registro.email_enviado,
    )
