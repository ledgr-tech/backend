import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConsumoEmpresaApagada(Base):
    """Quanto as empresas apagadas pela limpeza (issue #215) consumiram, por conta
    e mês civil de São Paulo (`AAAA-MM`). A limpeza apaga as execuções de
    conciliação da empresa, e a franquia do mês e o uso do `/admin` são contados
    nelas: sem esta tabela, os números cairiam 30 dias depois da remoção.

    `extratos_do_banco` é a unidade da franquia: os extratos do banco com a
    primeira execução naquele mês. `execucoes` é o total de execuções do mês,
    para o `conciliacoes_total` do `/admin`. Só números, nada da empresa. Duas
    empresas apagadas da mesma conta somam na mesma linha
    (app/services/exclusao.py). Sai junto com a conta."""

    __tablename__ = "consumo_empresas_apagadas"
    __table_args__ = (
        CheckConstraint(
            "extratos_do_banco >= 0 AND execucoes >= 0",
            name="ck_consumo_empresas_apagadas_positivo",
        ),
    )

    conta_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contas.id"), primary_key=True
    )
    mes: Mapped[str] = mapped_column(String(7), primary_key=True)
    extratos_do_banco: Mapped[int] = mapped_column(Integer, nullable=False)
    execucoes: Mapped[int] = mapped_column(Integer, nullable=False)
