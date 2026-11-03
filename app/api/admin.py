"""Painel administrativo interno (issue #156, ADR-025).

Só para o operador do Ledgr (`operadores_ledgr`), conferido no banco a cada
requisição por `obter_operador`: quem não é operador recebe 403, inclusive o
admin de qualquer conta, e o operador com login de mais de
`LOGIN_RECENTE_OPERADOR` recebe 401 (o front leva ao login de novo). Ignora
`X-Ledgr-Empresa` e não depende da conta do operador, nem de ela estar
inativa (app/core/conta_inativa.py deixa o `/admin` de fora).

Toda requisição com sessão válida grava uma linha em `acessos_admin`, numa
sessão própria e antes da resposta, inclusive as negadas: o usuário, o
operador, o método, o modelo da rota (nunca a URL com a busca), a conta
consultada e o resultado. Guardado por `RETENCAO_ACESSOS`; cada acesso
apaga até `LIMPEZA_POR_ACESSO` linhas vencidas.

- `GET /admin/contas`: busca (razão social sem acento, CNPJ com ou sem
  máscara, e-mail do admin), filtro por `situacao` (`sem_plano`, `teste`,
  `teste_encerrado`, `ativo`, `pagamento_pendente` e `assinatura_encerrada`
  entre as contas não inativas; `inativa`, as inativas), `ordem` (`criada_em`, mais novas primeiro; `teste_termina_em`,
  o fim mais próximo primeiro, sem teste no fim), `limit` e `offset`.
- `GET /admin/contas/{conta_id}`: o item, o admin (nome e e-mail, o contato
  comercial), a inativação, o último login, o uso e os últimos eventos.
- `POST /admin/contas/{conta_id}/inativar` e `/reativar` (`motivo`) e
  `/piloto` (`ativo` e `motivo`): gravam o estado em `contas` e um evento em
  `eventos_conta`. Repetir dá 409.

O uso são só contagens: empresas, usuários ativos, convites pendentes,
conciliações no total, extratos do banco conciliados pela primeira vez em
cada um dos últimos 3 meses (a unidade de cobrança da #122; mês civil de São
Paulo, o da franquia das issues #166 e #167),
chaves de API, conexões MCP e links públicos ativos. Nunca lançamento,
descrição, justificativa, nome de arquivo nem dado das empresas clientes.

Logs só com ids e o resultado, nunca nome, e-mail nem o termo buscado.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session

from app.api.empresas import _MASCARA_CNPJ, _SEM_ACENTO_PYTHON, _nome_normalizado
from app.core import plano
from app.core.auth import SessaoAutenticada, _agora, obter_sessao_autenticada
from app.core.database import SessionLocal, get_db
from app.core.rate_limit import (
    LIMITE_ADMIN_ACAO_POR_USUARIO,
    LIMITE_ADMIN_LEITURA_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import (
    AcessoAdmin,
    AssinaturaStripe,
    ChaveApi,
    ConexaoMcp,
    Conta,
    Convite,
    Empresa,
    EventoConta,
    ExecucaoConciliacao,
    Fechamento,
    LinkRelatorio,
    OperadorLedgr,
    Usuario,
)
from app.services.convites import condicoes_pendente
from app.services.plano import FUSO_DA_FRANQUIA

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

LOGIN_RECENTE_OPERADOR = timedelta(hours=2)
RETENCAO_ACESSOS = timedelta(days=365)
LIMPEZA_POR_ACESSO = 500
MESES_DE_USO = 3
EVENTOS_NO_DETALHE = 20
SO_OPERADOR = "Acesso restrito à equipe do Ledgr."
LOGIN_ANTIGO = "Entre de novo para usar o painel."
CONTA_NAO_ENCONTRADA = "Conta não encontrada."

SituacaoDoFiltro = Literal[
    "sem_plano",
    "teste",
    "teste_encerrado",
    "ativo",
    "pagamento_pendente",
    "assinatura_encerrada",
    "inativa",
]
Ordem = Literal["criada_em", "teste_termina_em"]


@dataclass(frozen=True)
class OperadorAutenticado:
    operador: OperadorLedgr
    usuario: Usuario


def _registrar_acesso(
    usuario_id: uuid.UUID,
    operador_id: uuid.UUID | None,
    request: Request,
    resultado: str,
) -> None:
    """Grava o acesso numa sessão própria, já com commit, para valer mesmo
    quando a requisição termina em erro. Apaga os vencidos aos poucos."""
    rota = getattr(request.scope.get("route"), "path", None) or request.url.path
    try:
        conta_consultada = uuid.UUID(str(request.path_params.get("conta_id")))
    except ValueError:
        conta_consultada = None
    with SessionLocal() as db:
        vencidos = (
            select(AcessoAdmin.id)
            .where(AcessoAdmin.em < func.now() - RETENCAO_ACESSOS)
            .order_by(AcessoAdmin.em)
            .limit(LIMPEZA_POR_ACESSO)
            .scalar_subquery()
        )
        db.execute(delete(AcessoAdmin).where(AcessoAdmin.id.in_(vencidos)))
        db.add(
            AcessoAdmin(
                usuario_id=usuario_id,
                operador_id=operador_id,
                metodo=request.method,
                rota=rota[:200],
                conta_consultada_id=conta_consultada,
                resultado=resultado,
            )
        )
        db.commit()
    logger.info(
        "admin_acesso resultado=%s usuario_id=%s conta_consultada_id=%s",
        resultado,
        usuario_id,
        conta_consultada,
    )


def obter_operador(
    request: Request,
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
    db: Annotated[Session, Depends(get_db)],
) -> OperadorAutenticado:
    usuario = sessao.usuario
    operador = db.scalar(
        select(OperadorLedgr).where(
            OperadorLedgr.usuario_id == usuario.id, OperadorLedgr.removido_em.is_(None)
        )
    )
    if operador is None:
        _registrar_acesso(usuario.id, None, request, "nao_operador")
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=SO_OPERADOR)
    if _agora().timestamp() - sessao.auth_time > LOGIN_RECENTE_OPERADOR.total_seconds():
        _registrar_acesso(usuario.id, operador.id, request, "login_antigo")
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail=LOGIN_ANTIGO)
    _registrar_acesso(usuario.id, operador.id, request, "permitido")
    return OperadorAutenticado(operador=operador, usuario=usuario)


# --- respostas ------------------------------------------------------------------------


class ContaItemResponse(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str
    plano: str | None
    situacao: plano.Situacao
    teste_termina_em: datetime | None
    assinatura_ativa: bool
    inativa: bool
    criada_em: datetime


class ContaListaResponse(BaseModel):
    total: int
    limit: int
    offset: int
    itens: list[ContaItemResponse]


class AdminDaContaResponse(BaseModel):
    nome: str
    email: str


class InativacaoResponse(BaseModel):
    em: datetime
    motivo: str


class UsoNoMesResponse(BaseModel):
    mes: str
    extratos_do_banco: int


class UsoResponse(BaseModel):
    empresas: int
    usuarios_ativos: int
    convites_pendentes: int
    conciliacoes_total: int
    extratos_do_banco_por_mes: list[UsoNoMesResponse]
    chaves_api_ativas: int
    conexoes_mcp_ativas: int
    links_publicos_ativos: int


class EventoResponse(BaseModel):
    tipo: str
    motivo: str
    operador: str | None
    origem: str
    em: datetime


class ContaDetalheResponse(ContaItemResponse):
    admin: AdminDaContaResponse | None
    inativacao: InativacaoResponse | None
    ultimo_login_em: datetime | None
    uso: UsoResponse
    eventos: list[EventoResponse]


class MotivoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo: Annotated[str, Field(max_length=500)]

    @field_validator("motivo")
    @classmethod
    def _preenchido(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("informe o motivo")
        return valor


class PilotoRequest(MotivoRequest):
    ativo: bool


# --- consultas ------------------------------------------------------------------------


def _situacao(agora: datetime):
    """A situação comercial em SQL, pela mesma regra de `situacao_da_conta`."""
    status = (
        select(AssinaturaStripe.status)
        .where(AssinaturaStripe.conta_id == Conta.id)
        .scalar_subquery()
    )
    return case(
        (Conta.assinatura_ativa, "ativo"),
        (status.in_(plano.STATUS_ATIVOS), "ativo"),
        (status.in_(plano.STATUS_PAGAMENTO_PENDENTE), "pagamento_pendente"),
        (status.in_(plano.STATUS_ENCERRADOS), "assinatura_encerrada"),
        (Conta.teste_termina_em.is_(None), "sem_plano"),
        (Conta.teste_termina_em > agora, "teste"),
        else_="teste_encerrado",
    )


def _admin_da_conta():
    return and_(Usuario.conta_id == Conta.id, Usuario.papel == "admin")


def _filtro_da_busca(busca: str | None):
    texto = (busca or "").strip()
    if not texto:
        return None
    condicoes = [
        _nome_normalizado(Conta.razao_social).contains(
            texto.translate(_SEM_ACENTO_PYTHON).lower(), autoescape=True
        ),
        Conta.id.in_(
            select(Usuario.conta_id).where(
                Usuario.papel == "admin",
                func.lower(Usuario.email).contains(texto.lower(), autoescape=True),
            )
        ),
    ]
    cnpj = _MASCARA_CNPJ.sub("", texto).upper()
    if cnpj.isalnum() and any(c.isdigit() for c in cnpj):
        condicoes.append(Conta.cnpj.contains(cnpj, autoescape=True))
    return or_(*condicoes)


def _em_utc(instante: datetime) -> datetime:
    return instante if instante.tzinfo else instante.replace(tzinfo=UTC)


def _item(conta: Conta, situacao: str) -> dict:
    return {
        "id": conta.id,
        "razao_social": conta.razao_social,
        "cnpj": conta.cnpj,
        "plano": conta.plano,
        "situacao": situacao,
        "teste_termina_em": conta.teste_termina_em,
        "assinatura_ativa": conta.assinatura_ativa,
        "inativa": conta.inativada_em is not None,
        "criada_em": _em_utc(conta.criado_em),
    }


def _meses(agora: datetime) -> list[str]:
    local = agora.astimezone(FUSO_DA_FRANQUIA)
    ano, mes = local.year, local.month
    meses = []
    for _ in range(MESES_DE_USO):
        meses.append(f"{ano:04d}-{mes:02d}")
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    return meses


def _uso(db: Session, conta_id: uuid.UUID, agora: datetime) -> UsoResponse:
    empresas = select(Empresa.id).where(Empresa.conta_id == conta_id).scalar_subquery()

    def contar(modelo, *condicoes) -> int:
        return db.scalar(select(func.count()).select_from(modelo).where(*condicoes))

    primeiras = (
        select(
            ExecucaoConciliacao.extrato_banco_id,
            func.min(ExecucaoConciliacao.criado_em).label("primeira"),
        )
        .where(ExecucaoConciliacao.empresa_id.in_(empresas))
        .group_by(ExecucaoConciliacao.extrato_banco_id)
        .subquery()
    )
    mes = func.to_char(func.timezone(FUSO_DA_FRANQUIA.key, primeiras.c.primeira), "YYYY-MM")
    meses = _meses(agora)
    por_mes = dict(db.execute(select(mes, func.count()).where(mes.in_(meses)).group_by(mes)).all())
    return UsoResponse(
        empresas=contar(Empresa, Empresa.conta_id == conta_id),
        usuarios_ativos=contar(
            Usuario, Usuario.conta_id == conta_id, Usuario.removido_em.is_(None)
        ),
        convites_pendentes=contar(Convite, Convite.conta_id == conta_id, *condicoes_pendente()),
        conciliacoes_total=contar(
            ExecucaoConciliacao, ExecucaoConciliacao.empresa_id.in_(empresas)
        ),
        extratos_do_banco_por_mes=[
            UsoNoMesResponse(mes=m, extratos_do_banco=por_mes.get(m, 0)) for m in meses
        ],
        chaves_api_ativas=contar(
            ChaveApi, ChaveApi.conta_id == conta_id, ChaveApi.revogada_em.is_(None)
        ),
        conexoes_mcp_ativas=contar(
            ConexaoMcp, ConexaoMcp.conta_id == conta_id, ConexaoMcp.revogada_em.is_(None)
        ),
        links_publicos_ativos=db.scalar(
            select(func.count())
            .select_from(LinkRelatorio)
            .join(Fechamento, Fechamento.id == LinkRelatorio.fechamento_id)
            .where(
                LinkRelatorio.empresa_id.in_(empresas),
                LinkRelatorio.revogado_em.is_(None),
                LinkRelatorio.expira_em > func.now(),
                Fechamento.reaberto_em.is_(None),
            )
        ),
    )


def _conta(db: Session, conta_id: uuid.UUID, *, travar: bool = False) -> Conta:
    consulta = select(Conta).where(Conta.id == conta_id)
    if travar:
        consulta = consulta.with_for_update()
    conta = db.scalar(consulta)
    if conta is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=CONTA_NAO_ENCONTRADA)
    return conta


def _detalhe(db: Session, conta: Conta) -> ContaDetalheResponse:
    agora = plano.agora()
    admin = db.execute(
        select(Usuario.nome, Usuario.email).where(
            _admin_da_conta(), Conta.id == conta.id, Usuario.removido_em.is_(None)
        )
    ).first()
    eventos = db.scalars(
        select(EventoConta)
        .where(EventoConta.conta_id == conta.id)
        .order_by(EventoConta.em.desc(), EventoConta.id.desc())
        .limit(EVENTOS_NO_DETALHE)
    ).all()
    return ContaDetalheResponse(
        **_item(conta, plano.situacao_da_conta(conta, agora)),
        admin=AdminDaContaResponse(nome=admin.nome, email=admin.email) if admin else None,
        inativacao=(
            InativacaoResponse(em=conta.inativada_em, motivo=conta.motivo_inativacao)
            if conta.inativada_em is not None
            else None
        ),
        ultimo_login_em=db.scalar(
            select(func.max(Usuario.ultimo_login_em)).where(Usuario.conta_id == conta.id)
        ),
        uso=_uso(db, conta.id, agora),
        eventos=[
            EventoResponse(
                tipo=evento.tipo,
                motivo=evento.motivo,
                operador=evento.operador_nome,
                origem=evento.origem,
                em=evento.em,
            )
            for evento in eventos
        ],
    )


def _evento(db: Session, conta: Conta, tipo: str, motivo: str, quem: OperadorAutenticado) -> None:
    db.add(
        EventoConta(
            conta_id=conta.id,
            tipo=tipo,
            motivo=motivo,
            operador_id=quem.operador.id,
            operador_nome=quem.usuario.nome,
            origem="painel",
        )
    )


def _conflito(detalhe: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detalhe)


# --- rotas ----------------------------------------------------------------------------


@router.get("/contas", response_model=ContaListaResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def listar_contas(
    request: Request,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
    busca: Annotated[str | None, Query(max_length=100)] = None,
    situacao: Annotated[SituacaoDoFiltro | None, Query()] = None,
    ordem: Annotated[Ordem, Query()] = "criada_em",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContaListaResponse:
    situacao_sql = _situacao(plano.agora()).label("situacao")
    consulta = select(Conta, situacao_sql, func.count().over().label("total"))
    filtro = _filtro_da_busca(busca)
    if filtro is not None:
        consulta = consulta.where(filtro)
    if situacao == "inativa":
        consulta = consulta.where(Conta.inativada_em.is_not(None))
    elif situacao is not None:
        consulta = consulta.where(Conta.inativada_em.is_(None), situacao_sql == situacao)
    if ordem == "teste_termina_em":
        consulta = consulta.order_by(Conta.teste_termina_em.asc().nulls_last(), Conta.id)
    else:
        consulta = consulta.order_by(Conta.criado_em.desc(), Conta.id.desc())
    linhas = db.execute(consulta.limit(limit).offset(offset)).all()
    return ContaListaResponse(
        total=linhas[0].total if linhas else 0,
        limit=limit,
        offset=offset,
        itens=[ContaItemResponse(**_item(linha.Conta, linha.situacao)) for linha in linhas],
    )


@router.get("/contas/{conta_id}", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_LEITURA_POR_USUARIO, key_func=chave_por_usuario)
def detalhar_conta(
    request: Request,
    conta_id: uuid.UUID,
    _operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    return _detalhe(db, _conta(db, conta_id))


@router.post("/contas/{conta_id}/inativar", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def inativar_conta(
    request: Request,
    conta_id: uuid.UUID,
    corpo: MotivoRequest,
    operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    conta = _conta(db, conta_id, travar=True)
    if conta.inativada_em is not None:
        raise _conflito("A conta já está inativa.")
    conta.inativada_em = func.now()
    conta.motivo_inativacao = corpo.motivo
    _evento(db, conta, "inativada", corpo.motivo, operador)
    db.commit()
    db.refresh(conta)
    logger.info("admin_conta acao=inativada conta_id=%s", conta.id)
    return _detalhe(db, conta)


@router.post("/contas/{conta_id}/reativar", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def reativar_conta(
    request: Request,
    conta_id: uuid.UUID,
    corpo: MotivoRequest,
    operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    conta = _conta(db, conta_id, travar=True)
    if conta.inativada_em is None:
        raise _conflito("A conta não está inativa.")
    conta.inativada_em = None
    conta.motivo_inativacao = None
    _evento(db, conta, "reativada", corpo.motivo, operador)
    db.commit()
    db.refresh(conta)
    logger.info("admin_conta acao=reativada conta_id=%s", conta.id)
    return _detalhe(db, conta)


@router.post("/contas/{conta_id}/piloto", response_model=ContaDetalheResponse)
@limiter.limit(LIMITE_ADMIN_ACAO_POR_USUARIO, key_func=chave_por_usuario)
def definir_piloto(
    request: Request,
    conta_id: uuid.UUID,
    corpo: PilotoRequest,
    operador: Annotated[OperadorAutenticado, Depends(obter_operador)],
    db: Annotated[Session, Depends(get_db)],
) -> ContaDetalheResponse:
    conta = _conta(db, conta_id, travar=True)
    if conta.assinatura_ativa == corpo.ativo:
        raise _conflito(
            "O piloto já está ligado nesta conta."
            if corpo.ativo
            else "O piloto já está desligado nesta conta."
        )
    conta.assinatura_ativa = corpo.ativo
    _evento(
        db, conta, "piloto_ligado" if corpo.ativo else "piloto_desligado", corpo.motivo, operador
    )
    db.commit()
    db.refresh(conta)
    logger.info("admin_conta acao=piloto ativo=%s conta_id=%s", corpo.ativo, conta.id)
    return _detalhe(db, conta)
