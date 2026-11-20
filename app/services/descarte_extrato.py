"""Descartar um extrato que nunca entrou em conciliação (issue #185).

Quando a normalização volta `concluido_com_erros`, a pessoa pode decidir
corrigir o arquivo em vez de seguir só com as linhas válidas. O extrato já
enviado fica em `GET /extratos` com `conciliado: false`, e `DELETE
/extratos/{id}` (app/api/extratos.py) o tira de lá.

Só se descarta o extrato da empresa ativa que ninguém usou:

- fora de toda execução de conciliação, como extrato do banco e como extrato
  do sistema (é o `conciliado` da listagem);
- por segurança, também fora de `conciliacoes` e de `decisoes_linha`, mesmo que
  a execução não exista mais: quem tem qualquer um desses registros já foi
  conciliado, e a FK dessas tabelas barraria o delete de qualquer jeito;
- fora de "pendente" e "processando": a normalização ainda vai gravar os
  lançamentos dele.

O rascunho da importação via IA (issue #158) responde como inexistente, como
em `GET /extratos/{id}`: ele tem rota própria (`DELETE /importacoes-ia/{id}`,
app/services/importacao_ia/rascunho.py), com a validade de 7 dias.

Concorrência: a checagem e o delete saem na mesma transação, com a trava do
extrato do banco (app/services/travas.py, a mesma da conciliação e das
decisões) e `SELECT ... FOR UPDATE` na linha do extrato. `conciliar_extratos`
pega a trava e um `FOR KEY SHARE` nas linhas dos dois extratos antes de gravar
a execução (`FOR UPDATE` e `FOR KEY SHARE` se excluem): ou a conciliação vem
primeiro e o descarte vê a execução (409), ou o descarte vem primeiro e a
conciliação encontra o extrato inexistente (404). Nunca sobra referência
órfã. A ordem das travas é a de sempre, a trava e depois a linha.

As FKs de `lancamentos` e `linhas_invalidas` para `extratos` não têm ON DELETE
CASCADE, e nenhuma migration foi criada para isso: o delete é explícito, na
ordem filhos e depois o extrato. O original no R2 não é tocado aqui: quem
chama o apaga depois do commit, e devolve a chave por `ExtratoDescartado`.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Conciliacao,
    DecisaoLinha,
    ExecucaoConciliacao,
    Extrato,
    Lancamento,
    LinhaInvalida,
)
from app.models.extrato import STATUS_RASCUNHO
from app.services.travas import travar_extrato_banco

MENSAGEM_EXTRATO_NAO_ENCONTRADO = "Extrato não encontrado."
MENSAGEM_EXTRATO_USADO = "Este extrato já foi usado em uma conciliação e não pode ser descartado."
MENSAGEM_EXTRATO_IMPORTANDO = (
    "Este extrato ainda está sendo importado. Aguarde terminar para descartá-lo."
)
# O extrato ainda vai receber os lançamentos da normalização.
STATUS_EM_IMPORTACAO = frozenset({"pendente", "processando"})


class ExtratoNaoEncontrado(LookupError):
    """Inexistente, de outra empresa ou rascunho da importação via IA."""


class ExtratoNaoDescartavel(Exception):
    """O extrato existe, mas não pode ser descartado. `mensagem` é o texto que
    a rota devolve como veio, em português."""

    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


@dataclass(frozen=True)
class ExtratoDescartado:
    extrato_id: uuid.UUID
    arquivo_chave: str | None


def extrato_foi_usado(db: Session, extrato_id: uuid.UUID) -> bool:
    """O extrato aparece em alguma execução, conciliação ou decisão, como banco
    ou como sistema. Não olha a empresa: a referência vale de onde vier."""
    referencias = [
        exists().where(
            or_(
                modelo.extrato_banco_id == extrato_id,
                modelo.extrato_sistema_id == extrato_id,
            )
        )
        for modelo in (ExecucaoConciliacao, Conciliacao, DecisaoLinha)
    ]
    return bool(db.scalar(select(or_(*referencias))))


def descartar_extrato(
    db: Session, empresa_id: uuid.UUID, extrato_id: uuid.UUID
) -> ExtratoDescartado:
    """Apaga o extrato, os lançamentos e as linhas não lidas dele, sem commit.
    Quem chama dá o commit (e solta as travas) e, depois dele, apaga o original.

    Levanta `ExtratoNaoEncontrado` ou `ExtratoNaoDescartavel`; nos dois casos
    nada foi apagado, e quem chama deve encerrar a transação."""
    travar_extrato_banco(db, empresa_id, extrato_id)
    extrato = db.scalars(
        select(Extrato)
        .where(
            Extrato.id == extrato_id,
            Extrato.empresa_id == empresa_id,
            Extrato.status != STATUS_RASCUNHO,
        )
        .with_for_update()
        # O status que vale é o da linha travada, não o de uma leitura anterior.
        .execution_options(populate_existing=True)
    ).one_or_none()
    if extrato is None:
        raise ExtratoNaoEncontrado(MENSAGEM_EXTRATO_NAO_ENCONTRADO)
    if extrato.status in STATUS_EM_IMPORTACAO:
        raise ExtratoNaoDescartavel(MENSAGEM_EXTRATO_IMPORTANDO)
    if extrato_foi_usado(db, extrato.id):
        raise ExtratoNaoDescartavel(MENSAGEM_EXTRATO_USADO)

    descartado = ExtratoDescartado(extrato_id=extrato.id, arquivo_chave=extrato.arquivo_chave)
    db.execute(delete(Lancamento).where(Lancamento.extrato_id == extrato.id))
    db.execute(delete(LinhaInvalida).where(LinhaInvalida.extrato_id == extrato.id))
    db.execute(delete(Extrato).where(Extrato.id == extrato.id))
    return descartado
