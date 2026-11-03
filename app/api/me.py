"""Rotas da própria conta de quem está logado (issue #66, ADR-012).

`GET /me` devolve os dados da conta pro cabeçalho do front (issue #81) e os
métodos de login. Desde a issue #133 (ADR-019), a conta é o tenant: vêm
`conta_id`, o `papel` do usuário ('admin', 'gestor' ou 'analista'), a razão
social e o CNPJ da conta, e `empresas`, as empresas clientes da conta (id,
razão social e CNPJ), ordenadas pela razão social, para o seletor da empresa
ativa. Ignora o cabeçalho `X-Ledgr-Empresa`. `plano`, `teste_termina_em` (em
UTC) e `situacao` (issue #122) são da conta, para todos os usuários dela: a
mesma conta do `POST /empresa/plano` (app/api/plano.py) e do bloqueio do
`POST /conciliacoes`. Desde as issues #166 e #167 (ADR-026), `plano` é o da
assinatura quando a conta tem uma (qualquer status: `situacao` diz se
encerrou), senão a preferência gravada em `contas.plano`, senão `null` (nunca o
essencial dos limites); e `situacao` ganhou `pagamento_pendente` e
`assinatura_encerrada`. `assinatura` é `null` enquanto a conta não assinou
pela Stripe; senão traz `status` (o da Stripe), `plano`, `periodo`,
`periodo_atual_fim` (em UTC) e `cancelar_no_fim_do_periodo`.

`preferencias` (issue #147) traz o que a pessoa dispensou no primeiro uso:
`tutoriais_dispensados` (telas de `TELAS_DO_TUTORIAL`) e
`roteiro_dispensado`. São do usuário, não da empresa nem da conta, e vêm de
colunas da própria linha dele, sem consulta a mais. Usuário novo vem com a
lista vazia e `false`.

`PATCH /me/preferencias` muda essas preferências, para todos os papéis e
sem olhar a empresa ativa. Os dois campos são opcionais: o que vier
substitui o valor guardado, o que não vier fica como está, e o corpo vazio
devolve o estado atual sem mudar nada. Tela fora da lista, `null` ou lista
com mais itens do que o total de telas dá 422 no formato do FastAPI; os
repetidos saem, na ordem da primeira ocorrência. A resposta é o estado
guardado depois da mudança. Rate limit por usuário, como as outras rotas.

`limites` (issue #133) traz, para empresas, usuários e conciliações, quanto a
conta usa e o máximo do plano (`null` sem limite), com a mesma conta do 402 do
`POST /empresas`, do `POST /empresa/convites` e do `POST /conciliacoes`:
usuários são os ativos mais os convites pendentes; `conciliacoes` são as
primeiras conciliações de extrato do banco no mês civil de São Paulo, de todas
as empresas da conta (a franquia do plano, issues #166 e #167). O front mostra
os avisos de limite com isso.

`guarda_arquivo_original` (issue #139, ADR-023) diz se o upload desta conta
guarda o arquivo original agora: flag `ARQUIVO_ORIGINAL_HABILITADO` ligada, R2
e chave configurados e conta no plano avançado. O front avisa no upload com
isso. É a mesma regra do upload (app/services/arquivo_original.py).

Obsoletos, mantidos até o front migrar: `empresa_id`, igual a `conta_id` (o
valor que o front grava no token, que o backend lê como id da conta), e
`administrador`, igual a `papel == "admin"` (issue #69, ADR-015).

`POST /me/senha` troca a senha na hora, com a senha atual e a nova (mesma
regra do cadastro). As regras vêm da issue:

- Senha atual incorreta devolve 400 "Senha atual incorreta.", nunca 401: o
  frontend trata todo 401 como sessão expirada e deslogaria a pessoa por um
  erro de digitação. Não há risco de enumeração, o token já diz quem é.
- Conta sem senha (login pelo Google) devolve 409. Definir senha pra essa
  conta, ou vincular o Google a uma conta com senha, fica para depois do MVP
  (issue #73). Nenhuma troca acontece sem a senha atual: com um token
  roubado, isso viraria acesso permanente à conta.
- Rate limit por usuário (`sub`), não por IP (ver app/core/rate_limit.py).

A troca invalida os links de recuperação pendentes (um link antigo não
desfaz a troca) e manda o aviso de senha alterada por e-mail, depois da
resposta. Com o e-mail desligado, a troca vale do mesmo jeito, sem aviso.
A troca também encerra todas as sessões abertas, inclusive a de quem
trocou (issue #75): o token com login anterior deixa de valer. O backend
não devolve token novo: quem emite o JWT é o NextAuth, e o front
reautentica com a senha nova.

`DELETE /me` desativa a conta de quem está logado (issue #68, ADR-017). A
exclusão dos dados da empresa não tem rota: é processo interno, a pedido
(scripts/excluir_empresa.py).

- Conta com senha: `senha_atual` obrigatória e conferida. Errada ou ausente
  dá 400 "Senha atual incorreta.", nunca 401, pelo mesmo motivo da troca.
- Conta só do Google: não há senha para conferir, então vale o login
  recente, com `auth_time` de no máximo `LOGIN_RECENTE_SEGUNDOS` atrás
  (app/core/auth.py, mesmo relógio da #75). Senão, 409: o front pede o
  Google de novo e repete.
- Admin com outro usuário ativo na conta: 409, sugerindo a transferência do
  papel (`POST /empresa/admin/transferencia`, issue #133), que é o caminho
  para o admin sair com a equipe ficando. Sozinho, pode desativar; a conta
  fica sem acesso, e os dados só saem pelo script.
- Rate limit por usuário, como a troca de senha.

A desativação é `remover_usuario` (app/services/usuarios.py): a linha fica
com o nome, para o histórico de decisões; o e-mail é liberado; senha e
`google_sub` são apagados; as sessões são encerradas. Os convites pendentes
que a pessoa mandou são cancelados. Com o e-mail ligado, o aviso vai para o
e-mail que a conta tinha, depois do commit. Nunca loga e-mail nem nome: só o
resultado categorizado.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.auth import SenhaNova
from app.api.plano import resposta_do_plano
from app.api.senha import agendar_aviso_senha_alterada, invalidar_links_de_recuperacao
from app.core import senha
from app.core.auth import (
    SessaoAutenticada,
    encerrar_sessoes,
    login_recente,
    obter_sessao_autenticada,
    obter_usuario_autenticado,
)
from app.core.database import get_db
from app.core.plano import Situacao, limite_da_conta
from app.core.rate_limit import (
    LIMITE_CONSULTA_ME_POR_USUARIO,
    LIMITE_DESATIVACAO_CONTA_POR_USUARIO,
    LIMITE_PREFERENCIAS_POR_USUARIO,
    LIMITE_TROCA_SENHA_POR_USUARIO,
    chave_por_usuario,
    limiter,
)
from app.models import Convite, Empresa, Usuario
from app.models.usuario import TELAS_DO_TUTORIAL, Papel, TelaDoTutorial
from app.services.arquivo_original import (
    ArquivoOriginal,
    conta_guarda_o_original,
    obter_arquivo_original,
)
from app.services.convites import condicoes_pendente
from app.services.email import ProvedorDeEmail
from app.services.email.envio import enviar_e_registrar, obter_provedor_email_dependencia
from app.services.email.mensagens import mensagem_conta_desativada
from app.services.plano import conciliacoes_no_mes, empresas_usadas, usuarios_usados
from app.services.usuarios import remover_usuario

__all__ = [
    "LIMITE_CONSULTA_ME_POR_USUARIO",
    "LIMITE_DESATIVACAO_CONTA_POR_USUARIO",
    "LIMITE_PREFERENCIAS_POR_USUARIO",
    "LIMITE_TROCA_SENHA_POR_USUARIO",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["conta"])

# A confirmação da desativação. A senha incorreta é a mesma resposta na
# transferência do admin (app/api/equipe.py).
SENHA_INCORRETA = "Senha atual incorreta."
CONFIRMAR_COM_O_GOOGLE = "Entre de novo com o Google para confirmar a desativação."


class EmpresaDaConta(BaseModel):
    id: uuid.UUID
    razao_social: str
    cnpj: str


class LimiteEmpresas(BaseModel):
    usadas: int
    maximo: int | None


class LimiteUsuarios(BaseModel):
    usados: int
    maximo: int | None


class LimiteConciliacoes(BaseModel):
    usadas: int
    maximo: int | None


class Limites(BaseModel):
    empresas: LimiteEmpresas
    usuarios: LimiteUsuarios
    conciliacoes: LimiteConciliacoes


class AssinaturaDaConta(BaseModel):
    status: str
    plano: str
    periodo: str
    periodo_atual_fim: datetime | None
    cancelar_no_fim_do_periodo: bool


def _assinatura(conta) -> AssinaturaDaConta | None:
    assinatura = conta.assinatura_stripe
    if assinatura is None:
        return None
    fim = assinatura.periodo_atual_fim
    return AssinaturaDaConta(
        status=assinatura.status,
        plano=assinatura.plano,
        periodo=assinatura.periodo,
        periodo_atual_fim=fim.astimezone(UTC) if fim is not None else None,
        cancelar_no_fim_do_periodo=assinatura.cancelar_no_fim_do_periodo,
    )


class Preferencias(BaseModel):
    tutoriais_dispensados: list[TelaDoTutorial]
    roteiro_dispensado: bool


def _preferencias(usuario: Usuario) -> Preferencias:
    return Preferencias(
        tutoriais_dispensados=usuario.tutoriais_dispensados,
        roteiro_dispensado=usuario.roteiro_dispensado,
    )


class MeResponse(BaseModel):
    id: uuid.UUID
    conta_id: uuid.UUID
    empresa_id: uuid.UUID = Field(
        description="Obsoleto: igual a `conta_id` (issue #133). Use `conta_id`.",
        json_schema_extra={"deprecated": True},
    )
    nome: str
    email: str
    papel: Papel
    administrador: bool = Field(
        description='Obsoleto: igual a `papel == "admin"` (issue #133). Use `papel`.',
        json_schema_extra={"deprecated": True},
    )
    razao_social: str
    cnpj: str
    empresas: list[EmpresaDaConta]
    metodos_login: list[str]
    plano: str | None
    teste_termina_em: datetime | None
    situacao: Situacao
    assinatura: AssinaturaDaConta | None
    limites: Limites
    preferencias: Preferencias
    guarda_arquivo_original: bool


def _metodos_login(usuario: Usuario) -> list[str]:
    metodos = []
    if usuario.senha_hash is not None:
        metodos.append("senha")
    if usuario.google_sub is not None:
        metodos.append("google")
    return metodos


@router.get("", response_model=MeResponse)
@limiter.limit(LIMITE_CONSULTA_ME_POR_USUARIO, key_func=chave_por_usuario)
def obter_dados_da_conta(
    request: Request,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    servico: Annotated[ArquivoOriginal | None, Depends(obter_arquivo_original)],
) -> MeResponse:
    conta = usuario.conta  # a dependency já carregou o usuário
    empresas = db.scalars(
        select(Empresa)
        .where(Empresa.conta_id == conta.id)
        .order_by(Empresa.razao_social, Empresa.id)
    )
    return MeResponse(
        id=usuario.id,
        conta_id=conta.id,
        empresa_id=conta.id,
        nome=usuario.nome,
        email=usuario.email,
        papel=usuario.papel,
        administrador=usuario.papel == "admin",
        razao_social=conta.razao_social,
        cnpj=conta.cnpj,
        empresas=[
            EmpresaDaConta(id=empresa.id, razao_social=empresa.razao_social, cnpj=empresa.cnpj)
            for empresa in empresas
        ],
        metodos_login=_metodos_login(usuario),
        **resposta_do_plano(conta).model_dump(),
        assinatura=_assinatura(conta),
        limites=Limites(
            empresas=LimiteEmpresas(
                usadas=empresas_usadas(db, conta.id),
                maximo=limite_da_conta(conta, "empresas"),
            ),
            usuarios=LimiteUsuarios(
                usados=usuarios_usados(db, conta.id),
                maximo=limite_da_conta(conta, "usuarios"),
            ),
            conciliacoes=LimiteConciliacoes(
                usadas=conciliacoes_no_mes(db, conta.id),
                maximo=limite_da_conta(conta, "conciliacoes"),
            ),
        ),
        preferencias=_preferencias(usuario),
        guarda_arquivo_original=conta_guarda_o_original(conta, servico),
    )


class PreferenciasRequest(BaseModel):
    """Os campos ausentes ficam como estão. Sem `| None`: `null` explícito é
    422, e só o que veio no corpo (`model_fields_set`) é gravado."""

    model_config = ConfigDict(extra="forbid")

    tutoriais_dispensados: list[TelaDoTutorial] = Field(
        default=None, max_length=len(TELAS_DO_TUTORIAL)
    )
    roteiro_dispensado: bool = None

    @field_validator("tutoriais_dispensados")
    @classmethod
    def _sem_repetidos(cls, telas: list[str]) -> list[str]:
        return list(dict.fromkeys(telas))


@router.patch("/preferencias", response_model=Preferencias)
@limiter.limit(LIMITE_PREFERENCIAS_POR_USUARIO, key_func=chave_por_usuario)
def salvar_preferencias(
    request: Request,
    dados: PreferenciasRequest,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
) -> Preferencias:
    campos = dados.model_fields_set
    if "tutoriais_dispensados" in campos:
        usuario.tutoriais_dispensados = dados.tutoriais_dispensados
    if "roteiro_dispensado" in campos:
        usuario.roteiro_dispensado = dados.roteiro_dispensado
    resposta = _preferencias(usuario)  # antes do commit, que expira o objeto
    db.commit()
    return resposta


class TrocaSenhaRequest(BaseModel):
    senha_atual: str = Field(min_length=1)
    senha_nova: SenhaNova


@router.post("/senha", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_TROCA_SENHA_POR_USUARIO, key_func=chave_por_usuario)
def trocar_senha(
    request: Request,
    dados: TrocaSenhaRequest,
    tarefas: BackgroundTasks,
    usuario: Annotated[Usuario, Depends(obter_usuario_autenticado)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
) -> None:
    if usuario.senha_hash is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Esta conta entra pelo Google e não tem senha.",
        )
    if not senha.verificar(dados.senha_atual, usuario.senha_hash):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Senha atual incorreta.",
        )

    usuario.senha_hash = senha.gerar_hash(dados.senha_nova)
    encerrar_sessoes(usuario)
    invalidar_links_de_recuperacao(db, usuario.id)
    para, nome = usuario.email, usuario.nome  # antes do commit, que expira o objeto
    db.commit()
    agendar_aviso_senha_alterada(tarefas, provedor, para, nome)


class DesativacaoContaRequest(BaseModel):
    # Sem `min_length`: ausente ou vazia é senha incorreta (400), não 422.
    senha_atual: str | None = None


@router.delete("", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit(LIMITE_DESATIVACAO_CONTA_POR_USUARIO, key_func=chave_por_usuario)
def desativar_conta(
    request: Request,
    tarefas: BackgroundTasks,
    sessao: Annotated[SessaoAutenticada, Depends(obter_sessao_autenticada)],
    db: Annotated[Session, Depends(get_db)],
    provedor: Annotated[ProvedorDeEmail | None, Depends(obter_provedor_email_dependencia)],
    dados: DesativacaoContaRequest | None = None,
) -> None:
    usuario = sessao.usuario
    # FOR UPDATE: duas desativações, ou a desativação e a remoção pelo
    # admin, não passam as duas.
    db.refresh(usuario, with_for_update=True)
    if usuario.removido_em is not None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if usuario.senha_hash is not None:
        senha_atual = dados.senha_atual if dados is not None else None
        if not senha_atual or not senha.verificar(senha_atual, usuario.senha_hash):
            logger.info("desativacao_conta resultado=senha_incorreta")
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=SENHA_INCORRETA,
            )
    elif not login_recente(sessao.auth_time):
        logger.info("desativacao_conta resultado=login_antigo")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=CONFIRMAR_COM_O_GOOGLE,
        )

    # Antes de contar os outros usuários: o UPDATE trava os convites
    # pendentes, então um aceite em andamento termina antes (e a contagem
    # abaixo, numa consulta nova, já vê o usuário criado) ou encontra o
    # convite cancelado.
    db.execute(
        update(Convite)
        .where(Convite.convidado_por_id == usuario.id, *condicoes_pendente())
        .values(cancelado_em=func.now())
    )
    admin = usuario.papel == "admin"
    if admin and db.scalar(
        select(func.count()).where(
            Usuario.conta_id == usuario.conta_id,
            Usuario.removido_em.is_(None),
            Usuario.id != usuario.id,
        )
    ):
        db.rollback()
        logger.info("desativacao_conta resultado=administrador_com_usuarios")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Transfira o papel de admin antes de desativar a sua conta.",
        )

    # Antes da remoção, que troca o e-mail, e do commit, que expira o objeto.
    mensagem = mensagem_conta_desativada(
        para=usuario.email,
        nome=usuario.nome,
        razao_social_sem_acesso=usuario.conta.razao_social if admin else None,
        chave_idempotencia=f"conta_desativada/{usuario.id}",
    )
    remover_usuario(db, usuario)
    db.commit()
    logger.info("desativacao_conta resultado=desativada")

    if provedor is None:
        logger.info("aviso_conta_desativada resultado=desabilitado")
        return
    tarefas.add_task(enviar_e_registrar, provedor, mensagem, "aviso_conta_desativada")
