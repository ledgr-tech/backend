"""Bloqueio de login por conta, contra força bruta (issue #67).

O rate limit de app/core/rate_limit.py é por IP: quem distribui as tentativas
por muitos IPs testa senhas de uma mesma conta sem nunca bater nele. Aqui o
contador é por e-mail: `MAXIMO_FALHAS` senhas erradas para o mesmo e-mail
dentro de `JANELA_FALHAS` bloqueiam esse e-mail por `DURACAO_BLOQUEIO`, venha
a tentativa de onde vier. Durante o bloqueio o `POST /login` responde 429 sem
chamar o bcrypt, nem com a senha certa.

Vale igual para e-mail que existe e que não existe: o contador não consulta o
banco, então o bloqueio não revela quem tem conta (o mesmo cuidado do 401 único
do login). O preço é que qualquer pessoa consegue bloquear o login de um e-mail
alheio por 15 minutos errando a senha 5 vezes. O link de recuperação de senha
destrava (`POST /senha/redefinir` chama `limpar`).

A chave é o SHA-256 do e-mail normalizado, nunca o e-mail em texto: nada do
que fica em memória (nem num dump do processo) lista e-mails.

Limitações, aceitas para o MVP:

- fica em memória do processo: um deploy ou restart zera todos os contadores;
- vale por processo: com mais de uma réplica (ou mais de um worker do uvicorn)
  cada uma conta sozinha, e o limite real vira `MAXIMO_FALHAS` vezes o número
  de réplicas. Antes de escalar, isto precisa ir para o banco ou para um Redis;
- o teto de `TETO_CHAVES` descarta as entradas mais antigas quando estoura,
  inclusive um bloqueio em vigor. Para tirar o bloqueio de uma conta assim é
  preciso errar a senha de milhares de outros e-mails, e o rate limit por IP
  segura isso.

As rotas síncronas rodam no threadpool do FastAPI, então todo acesso ao estado
passa por `_trava`.
"""

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.auth import normalizar_email

# Regra de produto, como os limites de app/core/rate_limit.py. Cinco erros
# deixam espaço para quem esqueceu a senha tentar as variações de sempre; 15
# minutos tornam a força bruta inviável (no máximo 20 senhas por hora por
# conta) sem prender por muito tempo quem errou de verdade, que ainda tem o
# link de recuperação.
MAXIMO_FALHAS = 5
JANELA_FALHAS = timedelta(minutes=15)
DURACAO_BLOQUEIO = timedelta(minutes=15)
# Teto de memória: cada entrada guarda um hash e até 5 datas.
TETO_CHAVES = 10_000


@dataclass
class _Entrada:
    falhas: list[datetime] = field(default_factory=list)
    bloqueado_ate: datetime | None = None

    def vencida(self, agora: datetime) -> bool:
        bloqueio_vencido = self.bloqueado_ate is None or self.bloqueado_ate <= agora
        return bloqueio_vencido and all(falha <= agora - JANELA_FALHAS for falha in self.falhas)


_trava = threading.Lock()
# Ordem de inserção = ordem da última atualização (move_to_end a cada falha),
# então o começo do dicionário tem as entradas mais antigas.
_entradas: OrderedDict[str, _Entrada] = OrderedDict()


def _chave(email: str) -> str:
    return hashlib.sha256(normalizar_email(email).encode()).hexdigest()


def _agora() -> datetime:
    return datetime.now(UTC)


def _abrir_espaco(agora: datetime) -> None:
    """Com o teto atingido, tira as vencidas; se não bastar, as mais antigas."""
    if len(_entradas) < TETO_CHAVES:
        return
    for chave in [chave for chave, entrada in _entradas.items() if entrada.vencida(agora)]:
        del _entradas[chave]
    while len(_entradas) >= TETO_CHAVES:
        _entradas.popitem(last=False)


def bloqueado_ate(email: str) -> datetime | None:
    """Até quando o e-mail está bloqueado, ou None se não está."""
    chave = _chave(email)
    agora = _agora()
    with _trava:
        entrada = _entradas.get(chave)
        if entrada is None:
            return None
        if entrada.bloqueado_ate is not None and entrada.bloqueado_ate > agora:
            return entrada.bloqueado_ate
        if entrada.vencida(agora):
            del _entradas[chave]
        return None


def registrar_falha(email: str) -> bool:
    """Conta uma senha errada. Devolve True quando esta falha bloqueou o e-mail."""
    chave = _chave(email)
    agora = _agora()
    with _trava:
        entrada = _entradas.get(chave)
        if entrada is None:
            _abrir_espaco(agora)
            entrada = _entradas[chave] = _Entrada()
        else:
            _entradas.move_to_end(chave)
        entrada.falhas = [falha for falha in entrada.falhas if falha > agora - JANELA_FALHAS]
        entrada.falhas.append(agora)
        if len(entrada.falhas) < MAXIMO_FALHAS:
            return False
        entrada.falhas = []
        entrada.bloqueado_ate = agora + DURACAO_BLOQUEIO
        return True


def limpar(email: str) -> None:
    """Zera falhas e bloqueio do e-mail (login com sucesso, senha redefinida)."""
    chave = _chave(email)
    with _trava:
        _entradas.pop(chave, None)


def limpar_tudo() -> None:
    """Zera o estado inteiro. Para os testes, que rodam no mesmo processo."""
    with _trava:
        _entradas.clear()
