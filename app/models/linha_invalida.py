import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class LinhaInvalida(Base, TimestampMixin):
    """Registro persistido de uma linha/transação que o parser não
    conseguiu normalizar, mas que não invalidou o arquivo inteiro
    (issue #13).

    Não confundir com `app.parsers.tipos.ErroLinha` — aquele é o dataclass
    em memória que o parser devolve dentro do `ResultadoParsing`; este é o
    registro persistido a partir dele, um por `ErroLinha`, gravado pela
    normalização (ver app/services/normalizacao.py).

    `motivo` NUNCA inclui a `descricao` do lançamento — só mensagens sobre
    data/valor inválidos, que é tudo que os parsers de OFX/CSV geram hoje
    (ver app/parsers/ofx.py::parse_ofx e app/parsers/csv.py::parse_csv).
    Descrição de lançamento pode conter dado sensível (nome de pessoa
    física — ver 07-tecnico/arquitetura-tecnica.md, seção Segurança/LGPD);
    manter essa disciplina explícita aqui se `motivo` passar a vir de outro
    lugar no futuro.
    """

    __tablename__ = "linhas_invalidas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extrato_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extratos.id"), nullable=False, index=True
    )
    identificador: Mapped[str] = mapped_column(String, nullable=False)
    motivo: Mapped[str] = mapped_column(String, nullable=False)

    extrato: Mapped["Extrato"] = relationship(back_populates="linhas_invalidas")  # noqa: F821
