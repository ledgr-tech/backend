import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ExecucaoConciliacao(Base, TimestampMixin):
    """Histórico de execuções de conciliação por empresa (issue #27, ADR-010).

    Só de inserção: cada linha é uma chamada bem-sucedida de `POST
    /conciliacoes`, gravada por `conciliar_extratos`
    (app/services/matching.py) na mesma transação do motor, dentro do mesmo
    `pg_advisory_xact_lock` do par — nunca editada nem apagada depois.
    `conciliacoes` (ADR-007) continua guardando só a última rodada de cada
    par; esta tabela é o que permite ver rodadas anteriores.

    `tolerancia_dias` é a tolerância efetivamente aplicada NAQUELA rodada
    (`Configuracao.tolerancia_dias_default` da empresa no momento da
    execução, ou 0 quando a empresa não tem `Configuracao`), não a
    configuração atual — a configuração pode mudar depois da execução.

    As 7 colunas de contagem espelham as categorias de
    `app/services/matching.py` (motor exato, tolerância de data e as 5
    categorias de divergência). O CHECK `ck_execucoes_conciliacao_total_e_
    soma_das_categorias` é a correção do bug original (issue #27, ADR-010):
    antes, `total` contava os pares por tolerância mas o retorno do motor não
    incluía `match_tolerancia`, então a soma das categorias devolvidas ficava
    menor que `total` sempre que `tolerancia_dias` > 0. Guardar as duas
    pontas na mesma linha e travar a invariante por CHECK impede que o mesmo
    erro volte a passar em silêncio.

    Percentual de acerto NÃO é coluna: é calculado na leitura por
    `calcular_percentual_acerto` (app/services/execucoes.py), a partir das
    contagens desta linha.

    `empresa_id` é indexado (toda query filtra por tenant, ADR-004) e entra
    num índice composto com `criado_em` porque a listagem do `GET /execucoes`
    é sempre "as execuções desta empresa, mais recentes primeiro".
    """

    __tablename__ = "execucoes_conciliacao"
    __table_args__ = (
        CheckConstraint(
            "total >= 0 AND match_exato >= 0 AND match_tolerancia >= 0 AND duplicado >= 0 "
            "AND sem_correspondencia >= 0 AND tarifa_bancaria >= 0 AND divergente_valor >= 0 "
            "AND divergente_data >= 0",
            name="ck_execucoes_conciliacao_contagens_nao_negativas",
        ),
        CheckConstraint(
            "total = match_exato + match_tolerancia + duplicado + sem_correspondencia "
            "+ tarifa_bancaria + divergente_valor + divergente_data",
            name="ck_execucoes_conciliacao_total_e_soma_das_categorias",
        ),
        Index("ix_execucoes_conciliacao_empresa_id_criado_em", "empresa_id", "criado_em"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False, index=True
    )
    extrato_banco_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    extrato_sistema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False
    )
    tolerancia_dias: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    match_exato: Mapped[int] = mapped_column(Integer, nullable=False)
    match_tolerancia: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicado: Mapped[int] = mapped_column(Integer, nullable=False)
    sem_correspondencia: Mapped[int] = mapped_column(Integer, nullable=False)
    tarifa_bancaria: Mapped[int] = mapped_column(Integer, nullable=False)
    divergente_valor: Mapped[int] = mapped_column(Integer, nullable=False)
    divergente_data: Mapped[int] = mapped_column(Integer, nullable=False)
