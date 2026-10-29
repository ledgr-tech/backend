"""Tipos e interface da camada de IA (issue #28, ADR-011).

`ProvedorDeIA` é a interface que qualquer implementação de LLM precisa
cumprir — o GPT-6 Luna hoje (app/services/ia/openai_provider.py), o
Sabiazinho depois, em issue própria, atrás da mesma interface. É síncrona
de propósito: o endpoint do PR B (issue #28, tabela de cache e rotas) roda
`explicar_divergencia` em threadpool, pra não empurrar asyncio pro backend
só por essa chamada.

Nunca logar prompt, resposta do modelo, descrição de lançamento nem a
chave (ADR-011). A mensagem de `ProvedorIAIndisponivel` também nunca contém
corpo de resposta nem descrição — só o motivo categorizado.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

MotivoIndisponibilidade = Literal[
    "timeout", "http", "resposta_invalida", "nao_configurado", "cota_esgotada"
]


@dataclass(frozen=True)
class LancamentoContexto:
    """Recorte mínimo de um lançamento pro contexto da IA (ADR-011). A
    descrição chega aqui como veio do banco — só é mascarada dentro de
    `serializar_contexto` (app/services/ia/prompt.py), que é o único
    caminho do dado até o prompt."""

    data: date
    valor: Decimal
    tipo: str
    descricao: str


@dataclass(frozen=True)
class CandidatoContexto:
    """Um lançamento do outro lado do par, com a diferença já calculada
    pelo backend (ADR-011: a IA não faz conta, só usa o que já vem pronto).
    `diferenca_valor` é preenchido pra `divergente_valor` (mesma data,
    valor diferente); `diferenca_dias`, pra `divergente_data` (mesmo valor,
    data diferente). Só um dos dois é preenchido, conforme o status."""

    lancamento: LancamentoContexto
    diferenca_valor: Decimal | None
    diferenca_dias: int | None


@dataclass(frozen=True)
class ContextoDivergencia:
    """Contexto canônico de uma divergência (ADR-011). Montar isto a partir
    de `Conciliacao` e do que existe do outro lado do par é responsabilidade
    do PR B (issue #28) — aqui só o formato que a camada de IA consome.
    `candidatos` entra na ordem em que foi montado, nunca reordenado por
    `serializar_contexto`."""

    status: str
    motivo: str
    lancamento: LancamentoContexto
    candidatos: tuple[CandidatoContexto, ...]
    quantidade_mesmo_lado: int | None
    quantidade_outro_lado: int | None


@dataclass(frozen=True)
class RespostaIA:
    """Resultado de uma chamada bem-sucedida ao provedor."""

    texto: str
    tokens_entrada: int
    tokens_saida: int
    modelo: str


@dataclass(frozen=True)
class MensagemHistorico:
    """Um turno anterior da conversa com o assistente (issue #137), já
    mascarado. O backend não guarda conversa: o front manda a cada pergunta."""

    papel: Literal["usuario", "assistente"]
    texto: str


@dataclass(frozen=True)
class PerguntaAjuda:
    """O que vai ao provedor numa pergunta ao assistente da Central de ajuda
    (issue #137, ADR-020). Nenhum dado de lançamento, da empresa ou da conta:
    a Central serializada, a pergunta e o histórico já mascarados e, se veio,
    o id da tela em que a pessoa está."""

    central: str
    pergunta: str
    historico: tuple[MensagemHistorico, ...]
    tela: str | None


@dataclass(frozen=True)
class RespostaAjuda:
    """A saída estruturada do modelo, já conferida no formato. `citacoes` são
    pares (pagina, funcao) como o modelo devolveu; quem chama confere se
    existem no conteúdo atual."""

    resposta: str
    respondida: bool
    citacoes: tuple[tuple[str, str], ...]
    tokens_entrada: int
    tokens_saida: int
    modelo: str


class ProvedorDeIA(Protocol):
    """Interface que qualquer provedor de LLM precisa cumprir (ADR-011).
    Síncrona de propósito — ver docstring do módulo."""

    def explicar_divergencia(
        self, contexto: ContextoDivergencia, *, identificador_anonimo: str | None = None
    ) -> RespostaIA: ...

    def responder_ajuda(
        self, pergunta: PerguntaAjuda, *, identificador_anonimo: str | None = None
    ) -> RespostaAjuda: ...


class ProvedorIAIndisponivel(Exception):
    """Levantada quando o provedor não consegue responder. `motivo` é
    sempre uma das categorias fixas — nunca texto livre com corpo de
    resposta ou descrição: "timeout", "http", "resposta_invalida",
    "nao_configurado" ou "cota_esgotada" (429 da OpenAI por teto de gasto ou
    crédito acabado, issue #87). `status_http` só é preenchido pra
    motivo="http" quando existe um código de resposta, e pra
    "cota_esgotada" (sempre 429)."""

    def __init__(self, motivo: MotivoIndisponibilidade, status_http: int | None = None) -> None:
        self.motivo = motivo
        self.status_http = status_http
        super().__init__(f"Provedor de IA indisponível: {motivo}")
