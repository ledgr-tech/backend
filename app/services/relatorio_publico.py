"""Link público do relatório do fechamento (issue #165, ADR-024): token,
consulta pelo token e o relatório montado campo a campo.

- **Token:** `secrets.token_urlsafe(32)` (256 bits). Só o sha256 e os 8
  primeiros caracteres ficam no banco.
- **Link válido:** `link_valido` faz uma consulta só, pelo hash, com todas as
  condições juntas (não revogado, não vencido, fechamento sem reabertura e,
  desde a issue #156, conta não inativa).
  Qualquer motivo de recusa é a mesma ausência de linha, sem caminho
  diferente por motivo.
- **Relatório:** `relatorio` monta a resposta só com os campos de
  `CAMPOS_DO_RELATORIO` e `CAMPOS_DA_LINHA` (o mesmo que o relatório em PDF do
  front mostra, decisão do Eduardo registrada no ADR-024). Os números vêm do
  resumo congelado do fechamento; as linhas divergentes, das conciliações
  dos pares desse resumo, que não mudam com o mês fechado (ADR-014). Nada de
  id interno, e-mail, dado da conta, quem aprovou, explicação da IA nem linha
  conciliada. Até `TETO_DE_LINHAS_POR_PAR` linhas por par, com `truncada`.
  Três consultas, qualquer que seja o número de pares: empresa, linhas
  (com `row_number` por par) e eventos de decisão.

Nunca loga o token, o hash, descrição de lançamento nem justificativa.
"""

import hashlib
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.orm import Session, aliased

from app.models import (
    Conciliacao,
    Conta,
    DecisaoLinha,
    Empresa,
    Fechamento,
    Lancamento,
    Usuario,
)
from app.models.link_relatorio import LinkRelatorio
from app.services import decisoes

TAMANHO_PREFIXO = 8
TETO_DE_LINHAS_POR_PAR = 5000
CATEGORIAS_DE_DIVERGENCIA = (
    "duplicado",
    "sem_correspondencia",
    "tarifa_bancaria",
    "divergente_valor",
    "divergente_data",
)

# As chaves exatas da resposta pública. O teste compara com elas.
CAMPOS_DO_RELATORIO = {
    "empresa": ("razao_social", "cnpj"),
    "resumo": (
        "total",
        "conciliadas",
        "justificadas",
        "pendentes",
        "linhas_nao_lidas",
        "valor_em_aberto",
        "categorias",
    ),
    "par": ("arquivo_banco", "arquivo_sistema", "rodada", "justificadas", "pendentes", "truncada"),
    "link": ("expira_em",),
}
CAMPOS_DA_LINHA = (
    "data",
    "descricao",
    "valor_banco",
    "valor_sistema",
    "situacao",
    "justificativa",
    "justificada_por",
    "justificada_em",
)


@dataclass(frozen=True)
class TokenNovo:
    token: str
    prefixo: str
    hash: str


def hash_do_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def gerar_token() -> TokenNovo:
    token = secrets.token_urlsafe(32)
    return TokenNovo(token=token, prefixo=token[:TAMANHO_PREFIXO], hash=hash_do_token(token))


def ativo():
    """O link vale agora: não revogado e não vencido. A reabertura do
    fechamento é conferida à parte, na consulta pública e na listagem."""
    return LinkRelatorio.revogado_em.is_(None), LinkRelatorio.expira_em > func.now()


def link_valido(db: Session, token: str) -> tuple[LinkRelatorio, Fechamento] | None:
    linha = db.execute(
        select(LinkRelatorio, Fechamento)
        .join(
            Fechamento,
            (Fechamento.id == LinkRelatorio.fechamento_id)
            & (Fechamento.empresa_id == LinkRelatorio.empresa_id),
        )
        .join(Empresa, Empresa.id == LinkRelatorio.empresa_id)
        .join(Conta, Conta.id == Empresa.conta_id)
        .where(
            LinkRelatorio.hash == hash_do_token(token),
            *ativo(),
            Fechamento.reaberto_em.is_(None),
            # Conta inativa (issue #156): o mesmo 404 dos links inválidos.
            Conta.inativada_em.is_(None),
        )
    ).one_or_none()
    return (linha.LinkRelatorio, linha.Fechamento) if linha is not None else None


def registrar_acesso(db: Session, link: LinkRelatorio) -> None:
    """Soma um acesso e grava a hora, sem commit. Não guarda IP."""
    db.execute(
        update(LinkRelatorio)
        .where(LinkRelatorio.id == link.id)
        .values(acessos=LinkRelatorio.acessos + 1, ultimo_acesso_em=func.clock_timestamp())
    )


def revogar_do_fechamento(db: Session, fechamento: Fechamento, usuario: Usuario) -> int:
    """Revoga, por reabertura, os links ainda não revogados do fechamento, sem
    commit. Devolve quantos."""
    resultado = db.execute(
        update(LinkRelatorio)
        .where(
            LinkRelatorio.fechamento_id == fechamento.id,
            LinkRelatorio.revogado_em.is_(None),
        )
        .values(
            revogado_em=func.clock_timestamp(),
            revogado_por_id=usuario.id,
            revogado_por_nome=usuario.nome,
            motivo_revogacao="reabertura",
        )
    )
    return resultado.rowcount


def _linhas_divergentes(db: Session, empresa_id: uuid.UUID, pares: list[tuple]) -> list:
    """As linhas divergentes dos pares, até o teto + 1 por par (para saber
    se truncou), na ordem da listagem da conciliação: data, valor, id."""
    lanc_banco = aliased(Lancamento)
    lanc_sistema = aliased(Lancamento)
    data = func.coalesce(lanc_banco.data, lanc_sistema.data)
    valor = func.coalesce(lanc_banco.valor, lanc_sistema.valor)
    numerada = (
        select(
            Conciliacao.extrato_banco_id,
            Conciliacao.extrato_sistema_id,
            Conciliacao.status,
            Conciliacao.lancamento_banco_id,
            lanc_banco.data.label("banco_data"),
            lanc_banco.valor.label("banco_valor"),
            lanc_banco.descricao.label("banco_descricao"),
            lanc_sistema.data.label("sistema_data"),
            lanc_sistema.valor.label("sistema_valor"),
            lanc_sistema.descricao.label("sistema_descricao"),
            lanc_sistema.ocorrencia.label("sistema_ocorrencia"),
            func.row_number()
            .over(
                partition_by=(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id),
                order_by=(data, valor, Conciliacao.id),
            )
            .label("ordem"),
        )
        .outerjoin(lanc_banco, Conciliacao.lancamento_banco_id == lanc_banco.id)
        .outerjoin(lanc_sistema, Conciliacao.lancamento_sistema_id == lanc_sistema.id)
        .where(
            Conciliacao.empresa_id == empresa_id,
            tuple_(Conciliacao.extrato_banco_id, Conciliacao.extrato_sistema_id).in_(pares),
            Conciliacao.status.not_in(sorted(decisoes.STATUS_RESOLVIDOS)),
        )
        .subquery()
    )
    return db.execute(
        select(numerada)
        .where(numerada.c.ordem <= TETO_DE_LINHAS_POR_PAR + 1)
        .order_by(numerada.c.extrato_banco_id, numerada.c.extrato_sistema_id, numerada.c.ordem)
    ).all()


def _linha(linha, vigente) -> dict:
    so_do_sistema = linha.lancamento_banco_id is None
    aprovada = decisoes.justificativa_aprovada(vigente)
    return {
        "data": (linha.sistema_data if so_do_sistema else linha.banco_data).isoformat(),
        "descricao": linha.sistema_descricao if so_do_sistema else linha.banco_descricao,
        "valor_banco": None if so_do_sistema else str(linha.banco_valor),
        "valor_sistema": None if linha.sistema_valor is None else str(linha.sistema_valor),
        "situacao": linha.status,
        "justificativa": vigente.evento.texto if aprovada else None,
        "justificada_por": vigente.evento.autor_nome if aprovada else None,
        "justificada_em": vigente.evento.em.isoformat() if aprovada else None,
    }


def relatorio(db: Session, link: LinkRelatorio, fechamento: Fechamento) -> dict:
    empresa = db.get(Empresa, link.empresa_id)
    resumo = fechamento.resumo
    contagens = resumo["contagens"]
    pares_do_resumo = resumo["pares"]
    chaves = [
        (uuid.UUID(par["extrato_banco_id"]), uuid.UUID(par["extrato_sistema_id"]))
        for par in pares_do_resumo
    ]

    linhas_por_par: dict[tuple, list] = defaultdict(list)
    eventos: dict[tuple, list[DecisaoLinha]] = defaultdict(list)
    if chaves:
        for linha in _linhas_divergentes(db, empresa.id, chaves):
            linhas_por_par[(linha.extrato_banco_id, linha.extrato_sistema_id)].append(linha)
        for evento in db.scalars(
            select(DecisaoLinha)
            .where(
                DecisaoLinha.empresa_id == empresa.id,
                DecisaoLinha.extrato_banco_id.in_(sorted({banco for banco, _ in chaves})),
            )
            .order_by(DecisaoLinha.em, DecisaoLinha.id)
        ):
            eventos[(evento.extrato_banco_id, evento.chave)].append(evento)

    pares = []
    for par, chave_do_par in zip(pares_do_resumo, chaves, strict=True):
        linhas = linhas_por_par[chave_do_par]
        justificadas, pendentes = [], []
        for linha in linhas[:TETO_DE_LINHAS_POR_PAR]:
            chave = decisoes.chave_da_linha(
                linha.lancamento_banco_id,
                linha.sistema_valor,
                linha.sistema_data,
                linha.sistema_descricao,
                linha.sistema_ocorrencia,
            )
            vigente = decisoes.decisao_vigente(eventos.get((chave_do_par[0], chave), []))
            destino = justificadas if decisoes.justificativa_aprovada(vigente) else pendentes
            destino.append(_linha(linha, vigente))
        pares.append(
            {
                "arquivo_banco": par["nome_arquivo_banco"],
                "arquivo_sistema": par["nome_arquivo_sistema"],
                "rodada": par["rodada"],
                "justificadas": justificadas,
                "pendentes": pendentes,
                "truncada": len(linhas) > TETO_DE_LINHAS_POR_PAR,
            }
        )

    return {
        "empresa": {"razao_social": empresa.razao_social, "cnpj": empresa.cnpj},
        "competencia": fechamento.competencia,
        "situacao": "fechado_com_ressalva" if fechamento.ressalva else "fechado",
        "fechado_em": fechamento.fechado_em.isoformat(),
        "fechado_por": fechamento.fechado_por_nome,
        "ressalva": fechamento.ressalva,
        "resumo": {
            "total": contagens["total"],
            "conciliadas": contagens["match_exato"] + contagens["match_tolerancia"],
            "justificadas": resumo["justificadas"],
            "pendentes": resumo["pendentes"],
            "linhas_nao_lidas": resumo["linhas_nao_lidas"],
            "valor_em_aberto": resumo["valor_em_aberto"],
            "categorias": {
                categoria: contagens[categoria] for categoria in CATEGORIAS_DE_DIVERGENCIA
            },
        },
        "pares": pares,
        "link": {"expira_em": link.expira_em.isoformat()},
    }
