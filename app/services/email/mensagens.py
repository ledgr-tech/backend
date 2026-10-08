"""Textos dos e-mails transacionais (issue #66, ADR-012).

Cada mensagem sai em texto puro e em HTML simples, sem imagem nem
rastreamento. Tudo o que vem do usuário (o nome) é escapado no HTML; o
link é montado pelo backend, a partir de `FRONTEND_URL` e de um token
URL-safe.
"""

from html import escape

from app.services.email.base import MensagemEmail


def mensagem_recuperacao_senha(
    *,
    para: str,
    nome: str,
    link: str,
    validade_minutos: int,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    texto = (
        f"Olá, {nome}.\n\n"
        "Recebemos um pedido para redefinir a senha da sua conta na Ledgr. "
        "Para criar uma senha nova, abra o link abaixo:\n\n"
        f"{link}\n\n"
        f"O link vale por {validade_minutos} minutos e só pode ser usado uma vez.\n\n"
        "Se não foi você, ignore este e-mail: sua senha atual continua valendo.\n\n"
        "Equipe Ledgr"
    )
    link_html = escape(link, quote=True)
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        "<p>Recebemos um pedido para redefinir a senha da sua conta na Ledgr. "
        "Para criar uma senha nova, use o botão abaixo:</p>"
        f'<p><a href="{link_html}">Redefinir minha senha</a></p>'
        f"<p>Se o botão não funcionar, copie e cole este endereço no navegador:<br>"
        f"{link_html}</p>"
        f"<p>O link vale por {validade_minutos} minutos e só pode ser usado uma vez.</p>"
        "<p>Se não foi você, ignore este e-mail: sua senha atual continua valendo.</p>"
        "<p>Equipe Ledgr</p>"
    )
    return MensagemEmail(
        para=para,
        assunto="Redefinição de senha da sua conta Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_senha_alterada(*, para: str, nome: str, link_login: str | None) -> MensagemEmail:
    """Aviso depois de qualquer troca de senha: pela tela de configurações ou
    pelo link de recuperação. Quem não reconhece a troca precisa saber o que
    fazer, e o caminho é o "Esqueci a senha", que só depende do e-mail."""
    onde_entrar = f" na tela de login ({link_login})" if link_login else " na tela de login"
    texto = (
        f"Olá, {nome}.\n\n"
        "A senha da sua conta na Ledgr acabou de ser alterada.\n\n"
        "Se foi você, não precisa fazer nada.\n\n"
        "Se não foi você, alguém pode ter acesso à sua conta. Use o "
        f'"Esqueci a senha"{onde_entrar} para criar uma senha nova agora e '
        "avise o nosso suporte.\n\n"
        "Equipe Ledgr"
    )
    onde_entrar_html = (
        f' na <a href="{escape(link_login, quote=True)}">tela de login</a>'
        if link_login
        else " na tela de login"
    )
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        "<p>A senha da sua conta na Ledgr acabou de ser alterada.</p>"
        "<p>Se foi você, não precisa fazer nada.</p>"
        "<p>Se não foi você, alguém pode ter acesso à sua conta. Use o "
        f"<strong>Esqueci a senha</strong>{onde_entrar_html} para criar uma senha nova "
        "agora e avise o nosso suporte.</p>"
        "<p>Equipe Ledgr</p>"
    )
    return MensagemEmail(
        para=para,
        assunto="Sua senha da Ledgr foi alterada",
        texto=texto,
        html=html,
    )
