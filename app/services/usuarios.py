"""Remoção de usuário (issue #69, ADR-015).

Remover não apaga a linha: `decisoes_linha.usuario_id` é NOT NULL e sem
`ON DELETE`, e o histórico precisa continuar dizendo quem decidiu o quê. O
usuário removido:

- fica com `removido_em` preenchido e mantém o nome;
- perde a senha e o `google_sub`, então não entra mais por nenhum método;
- troca o e-mail por `removido+<id>@ledgr.invalid`, um marcador único que não
  é endereço de entrega, e o e-mail original fica livre para outro cadastro
  ou para um convite novo (que cria outro usuário: não há reativação);
- tem os links pendentes de `tokens_email` invalidados;
- tem as sessões encerradas (issue #75).

Toda rota autenticada recusa o removido com 401, já no token que ele tinha
(app/core/auth.py), e o `/login` responde como senha errada.

A mesma função serve para o próprio usuário desativar a conta (issue #68).
"""

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.auth import encerrar_sessoes
from app.models import TokenEmail, Usuario
from app.models.usuario import DOMINIO_EMAIL_REMOVIDO


def email_de_removido(usuario: Usuario) -> str:
    return f"removido+{usuario.id}@{DOMINIO_EMAIL_REMOVIDO}"


def remover_usuario(db: Session, usuario: Usuario) -> None:
    """Aplica a remoção na sessão, sem commit: quem chama decide a transação
    (e deve ter carregado o usuário com `FOR UPDATE`). Não confere permissão
    nem se é o admin da conta; isso é regra de quem chama."""
    usuario.removido_em = func.now()
    usuario.email = email_de_removido(usuario)
    usuario.senha_hash = None
    usuario.google_sub = None
    encerrar_sessoes(usuario)
    db.execute(
        update(TokenEmail)
        .where(TokenEmail.usuario_id == usuario.id, TokenEmail.usado_em.is_(None))
        .values(usado_em=func.now())
    )
