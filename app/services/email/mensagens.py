"""Textos dos e-mails transacionais (issues #66, #68, #69, #78, #149 e #169, ADR-012).

Cada mensagem sai em texto puro e em HTML de e-mail (tabelas e estilo inline,
porque os clientes de e-mail ignoram `<style>` e classes), sem rastreamento.
O visual segue o guia de estilo do Ledgr (issue #188): `_pagina` é o molde
único, e cada mensagem só entrega o miolo. O logo e o mascote são imagens
servidas pelo front (`<FRONTEND_URL>/email/*.png`); sem `FRONTEND_URL` que
sirva, o e-mail sai sem imagem. Tudo o que vem do usuário (nome, razão
social) é escapado no HTML; o link é montado pelo backend, a partir de
`FRONTEND_URL` e de um token URL-safe.
"""

from html import escape

from app.services.email.base import MensagemEmail

# Valores do guia de estilo (app/globals.css do front). O dourado é só traço
# (a borda do botão); o texto do botão usa o passo 700, que passa no contraste.
_FUNDO = "#f3f2f2"
_TEXTO = "#201f1d"
_SUAVE = "#6b6864"
_LINHA = "#e3e1de"
_DOURADO = "#b68235"
_DOURADO_700 = "#7d5411"
# Gmail e Outlook não carregam fonte web: caem em Arial e Georgia.
_FONTE = "Inter, -apple-system, 'Segoe UI', Arial, sans-serif"
_FONTE_TITULO = "Newsreader, Georgia, 'Times New Roman', serif"


def _p(conteudo_html: str) -> str:
    return f'<p style="margin:0 0 16px;">{conteudo_html}</p>'


def _botao(rotulo: str, url: str) -> str:
    """Contornado como o `.btn-primary` do app: borda dourada, texto escuro."""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 20px;">'
        f'<tr><td style="border:1px solid {_DOURADO};border-radius:16px;">'
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:11px 24px;'
        f'font-size:14px;font-weight:600;color:{_DOURADO_700};text-decoration:none;">'
        f"{escape(rotulo)}</a></td></tr></table>"
    )


def _endereco(url: str) -> str:
    """O link por extenso, para quem não consegue clicar no botão."""
    return (
        f'<p style="margin:0 0 16px;font-size:13px;color:{_SUAVE};">'
        "Se o botão não funcionar, copie e cole este endereço no navegador:<br>"
        f'<span style="word-break:break-all;">{escape(url, quote=True)}</span></p>'
    )


def _nota(conteudo_html: str) -> str:
    return (
        f'<p style="margin:0;font-size:13px;color:{_SUAVE};border-top:1px solid {_LINHA};'
        f'padding-top:16px;">{conteudo_html}</p>'
    )


def _aviso(conteudo_html: str) -> str:
    """Caixa neutra do "Não foi você?", o aviso de segurança que fecha os e-mails de conta."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 4px;"><tr>'
        f'<td style="background:{_FUNDO};border:1px solid {_LINHA};border-radius:10px;'
        f'padding:14px 16px;font-size:14px;line-height:1.55;color:{_TEXTO};">'
        f"{conteudo_html}</td></tr></table>"
    )


def _pagina(titulo: str, mascote: str, corpo: str) -> str:
    """Molde de todos os e-mails: assinatura no alto, cartão branco com o título
    e o mascote, e o rodapé. `titulo` é texto fixo; `mascote` é o nome do PNG em
    `<FRONTEND_URL>/email/` (envelope, joinha, comemorando, neutro)."""
    # Import preguiçoso, como em app/services/email/__init__.py: importar este
    # módulo não pode exigir a configuração.
    from app.core.frontend_url import frontend_url_para_link

    base = frontend_url_para_link()
    imagens = escape(f"{base}/email", quote=True) if base else None

    logo = (
        f'<td style="vertical-align:middle;padding-right:10px;"><img src="{imagens}/logo.png" '
        'width="40" alt="" style="display:block;border:0;height:auto;"></td>'
        if imagens
        else ""
    )
    figura = (
        '<td width="96" align="right" style="vertical-align:middle;padding-left:12px;">'
        f'<img src="{imagens}/{mascote}.png" width="88" alt="" '
        'style="display:block;border:0;height:auto;max-width:88px;"></td>'
        if imagens
        else ""
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{_FUNDO}" style="background:{_FUNDO};font-family:{_FONTE};">'
        '<tr><td align="center" style="padding:8px 0;">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:560px;">'
        '<tr><td style="padding:8px 4px 16px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>{logo}'
        f'<td style="vertical-align:middle;font-size:20px;font-weight:600;color:{_TEXTO};'
        'letter-spacing:-0.01em;">Ledgr</td>'
        '<td style="vertical-align:middle;padding-left:12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="border-left:1px solid {_LINHA};padding-left:12px;font-size:10px;'
        f'font-weight:500;letter-spacing:0.16em;text-transform:uppercase;color:{_SUAVE};">'
        "Conciliação bancária</td></tr></table></td>"
        "</tr></table></td></tr>"
        f'<tr><td bgcolor="#ffffff" style="background:#ffffff;border:1px solid {_LINHA};'
        "border-radius:10px;padding:32px 32px 28px;font-size:15px;line-height:1.6;"
        f'color:{_TEXTO};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 20px;"><tr>'
        f'<td style="vertical-align:middle;"><h1 style="margin:0;font-family:{_FONTE_TITULO};'
        f'font-size:24px;line-height:1.25;font-weight:500;color:{_TEXTO};">{titulo}</h1></td>'
        f"{figura}</tr></table>"
        f"{corpo}</td></tr>"
        f'<tr><td style="padding:16px 4px 0;font-size:12px;line-height:1.5;color:{_SUAVE};">'
        "Equipe Ledgr · Conciliação bancária</td></tr>"
        "</table></td></tr></table>"
    )


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
    html = _pagina(
        "Redefinir sua senha",
        "envelope",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            "Recebemos um pedido para redefinir a senha da sua conta na Ledgr. "
            "Para criar uma senha nova, use o botão abaixo."
        )
        + _botao("Redefinir minha senha", link)
        + _endereco(link)
        + _p(
            f"O link vale por <strong>{validade_minutos} minutos</strong> e só pode ser usado uma vez."
        )
        + _nota("Se não foi você, ignore este e-mail: sua senha atual continua valendo."),
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
        f' na <a href="{escape(link_login, quote=True)}" '
        f'style="color:{_TEXTO};text-decoration:underline;">tela de login</a>'
        if link_login
        else " na tela de login"
    )
    html = _pagina(
        "Sua senha foi alterada",
        "joinha",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            "A senha da sua conta na Ledgr acabou de ser alterada. Se foi você, não precisa fazer nada."
        )
        + _aviso(
            "<strong>Não foi você?</strong> Alguém pode ter acesso à sua conta. Use o "
            f"<strong>Esqueci a senha</strong>{onde_entrar_html} para criar uma senha nova "
            "agora e avise o nosso suporte."
        ),
    )
    return MensagemEmail(
        para=para,
        assunto="Sua senha da Ledgr foi alterada",
        texto=texto,
        html=html,
    )


def mensagem_boas_vindas(
    *,
    para: str,
    nome: str,
    razao_social: str,
    link_login: str | None,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Depois do cadastro (issue #78). O fim do texto cobre quem recebe sem ter
    criado a conta: alguém cadastrou o e-mail dessa pessoa."""
    onde_entrar = f" na tela de login ({link_login})" if link_login else " na tela de login"
    texto = (
        f"Olá, {nome}.\n\n"
        f"Sua conta na Ledgr foi criada para a empresa {razao_social}.\n\n"
        f"Para começar, entre{onde_entrar} e envie o extrato do banco e o extrato do "
        "seu sistema de gestão do mesmo período. A Ledgr cruza os dois e mostra o que "
        "bate e o que precisa da sua atenção.\n\n"
        "Se não foi você quem criou esta conta, ignore este e-mail e avise o nosso "
        "suporte.\n\n"
        "Equipe Ledgr"
    )
    onde_entrar_html = (
        f' na <a href="{escape(link_login, quote=True)}" '
        f'style="color:{_TEXTO};text-decoration:underline;">tela de login</a>'
        if link_login
        else " na tela de login"
    )
    html = _pagina(
        "Sua conta está pronta",
        "comemorando",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            f"Sua conta na Ledgr foi criada para a empresa <strong>{escape(razao_social)}</strong>."
        )
        + _p(
            f"Para começar, entre{onde_entrar_html} e envie o extrato do banco e o "
            "extrato do seu sistema de gestão do mesmo período. A Ledgr cruza os dois e "
            "mostra o que bate e o que precisa da sua atenção."
        )
        + (_botao("Entrar na Ledgr", link_login) if link_login else "")
        + _nota(
            "Se não foi você quem criou esta conta, ignore este e-mail e avise o nosso suporte."
        ),
    )
    return MensagemEmail(
        para=para,
        assunto="Boas-vindas à Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_convite(
    *,
    para: str,
    nome_de_quem_convidou: str,
    razao_social: str,
    link: str,
    validade_dias: int,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Convite para entrar numa empresa que já existe (issue #69). O
    convidado ainda não tem conta, então a mensagem não tem nome dele."""
    texto = (
        "Olá.\n\n"
        f"{nome_de_quem_convidou} convidou você para usar a Ledgr na empresa "
        f"{razao_social}.\n\n"
        "Para aceitar, abra o link abaixo e crie a sua senha:\n\n"
        f"{link}\n\n"
        f"O convite vale por {validade_dias} dias e só pode ser usado uma vez.\n\n"
        "Se você não esperava este convite, ignore este e-mail: nenhuma conta é "
        "criada sem o aceite.\n\n"
        "Equipe Ledgr"
    )
    html = _pagina(
        "Você foi convidado para a Ledgr",
        "envelope",
        _p("Olá.")
        + _p(
            f"<strong>{escape(nome_de_quem_convidou)}</strong> convidou você para usar a "
            f"Ledgr na empresa <strong>{escape(razao_social)}</strong>."
        )
        + _p("Para aceitar, use o botão abaixo e crie a sua senha.")
        + _botao("Aceitar o convite", link)
        + _endereco(link)
        + _p(
            f"O convite vale por <strong>{validade_dias} dias</strong> e só pode ser usado uma vez."
        )
        + _nota(
            "Se você não esperava este convite, ignore este e-mail: nenhuma conta é "
            "criada sem o aceite."
        ),
    )
    return MensagemEmail(
        para=para,
        assunto="Convite para usar a Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_conta_desativada(
    *,
    para: str,
    nome: str,
    razao_social_sem_acesso: str | None,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Depois de a pessoa desativar a própria conta (issue #68), para o e-mail
    que a conta tinha. Quem não reconhece a desativação precisa saber que foi
    feita. `razao_social_sem_acesso` vem quando era o administrador sozinho:
    a empresa fica sem acesso, e os dados dela só saem a pedido."""
    texto = (
        f"Olá, {nome}.\n\n"
        "A sua conta na Ledgr foi desativada a seu pedido. Ela não entra mais, e "
        "este e-mail fica livre para um cadastro novo.\n\n"
    )
    corpo_html = _p(f"Olá, {escape(nome)}.") + _p(
        "A sua conta na Ledgr foi desativada a seu pedido. Ela não entra mais, e "
        "este e-mail fica livre para um cadastro novo."
    )
    if razao_social_sem_acesso is not None:
        texto += (
            f"Os dados da empresa {razao_social_sem_acesso} continuam guardados, sem "
            "acesso de ninguém. Para pedir a exclusão deles, escreva ao nosso suporte.\n\n"
        )
        corpo_html += _p(
            f"Os dados da empresa <strong>{escape(razao_social_sem_acesso)}</strong> continuam "
            "guardados, sem acesso de ninguém. Para pedir a exclusão deles, escreva ao nosso "
            "suporte."
        )
    texto += "Se não foi você, avise o nosso suporte agora.\n\nEquipe Ledgr"
    corpo_html += _aviso("<strong>Não foi você?</strong> Avise o nosso suporte agora.")
    return MensagemEmail(
        para=para,
        assunto="Sua conta da Ledgr foi desativada",
        texto=texto,
        html=_pagina("Sua conta foi desativada", "neutro", corpo_html),
        chave_idempotencia=chave_idempotencia,
    )


def mascarar_email(email: str) -> str:
    """`nome@dominio.com` vira `n***@dominio.com`: o aviso ao e-mail atual diz
    para onde a troca vai sem entregar o endereço inteiro (issue #149)."""
    local, _, dominio = email.partition("@")
    return f"{local[:1]}***@{dominio}"


def mensagem_confirmacao_troca_email(
    *,
    para: str,
    nome: str,
    link: str,
    validade_minutos: int,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Link de confirmação da troca de e-mail, para o e-mail NOVO (issue #149):
    a troca só vale quando alguém que lê esse endereço abre o link."""
    texto = (
        f"Olá, {nome}.\n\n"
        "Recebemos um pedido para usar este e-mail na sua conta da Ledgr. Para "
        "confirmar a troca, abra o link abaixo:\n\n"
        f"{link}\n\n"
        f"O link vale por {validade_minutos} minutos e só pode ser usado uma vez. "
        "Depois da confirmação, você entra na Ledgr com este e-mail.\n\n"
        "Se não foi você, ignore este e-mail: nada muda na conta.\n\n"
        "Equipe Ledgr"
    )
    html = _pagina(
        "Confirme o seu e-mail novo",
        "envelope",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            "Recebemos um pedido para usar este e-mail na sua conta da Ledgr. Para "
            "confirmar a troca, use o botão abaixo."
        )
        + _botao("Confirmar o e-mail novo", link)
        + _endereco(link)
        + _p(
            f"O link vale por <strong>{validade_minutos} minutos</strong> e só pode ser usado "
            "uma vez. Depois da confirmação, você entra na Ledgr com este e-mail."
        )
        + _nota("Se não foi você, ignore este e-mail: nada muda na conta."),
    )
    return MensagemEmail(
        para=para,
        assunto="Confirme o seu e-mail novo na Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_troca_email_pedida(
    *,
    para: str,
    nome: str,
    email_novo: str,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Aviso ao e-mail ATUAL quando a troca é pedida (issue #149). O e-mail
    novo vai mascarado. Quem não pediu precisa agir antes da confirmação."""
    mascarado = mascarar_email(email_novo)
    texto = (
        f"Olá, {nome}.\n\n"
        f"Pediram a troca do e-mail da sua conta na Ledgr para {mascarado}. A troca "
        "só vale depois que alguém confirmar pelo link enviado ao e-mail novo.\n\n"
        "Se foi você, não precisa fazer nada aqui.\n\n"
        "Se não foi você, alguém sabe a sua senha: troque a senha agora e fale com o "
        "nosso suporte.\n\n"
        "Equipe Ledgr"
    )
    html = _pagina(
        "Pediram para trocar o seu e-mail",
        "envelope",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            "Pediram a troca do e-mail da sua conta na Ledgr para "
            f"<strong>{escape(mascarado)}</strong>. A troca só vale depois que alguém "
            "confirmar pelo link enviado ao e-mail novo."
        )
        + _p("Se foi você, não precisa fazer nada aqui.")
        + _aviso(
            "<strong>Não foi você?</strong> Alguém sabe a sua senha: troque a senha agora e "
            "fale com o nosso suporte."
        ),
    )
    return MensagemEmail(
        para=para,
        assunto="Pedido de troca do e-mail da sua conta Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_troca_email_concluida(
    *,
    para: str,
    nome: str,
    email_novo: str,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Aviso ao e-mail ANTIGO depois da confirmação (issue #149): a conta não
    entra mais com ele. Quem não reconhece a troca só tem o suporte."""
    mascarado = mascarar_email(email_novo)
    texto = (
        f"Olá, {nome}.\n\n"
        f"O e-mail da sua conta na Ledgr foi trocado para {mascarado}. A partir de "
        "agora, o login é com o e-mail novo, e as sessões abertas foram encerradas.\n\n"
        "Se foi você, não precisa fazer nada.\n\n"
        "Se não foi você, fale com o nosso suporte agora.\n\n"
        "Equipe Ledgr"
    )
    html = _pagina(
        "Seu e-mail foi trocado",
        "joinha",
        _p(f"Olá, {escape(nome)}.")
        + _p(
            "O e-mail da sua conta na Ledgr foi trocado para "
            f"<strong>{escape(mascarado)}</strong>. A partir de agora, o login é com o "
            "e-mail novo, e as sessões abertas foram encerradas."
        )
        + _p("Se foi você, não precisa fazer nada.")
        + _aviso("<strong>Não foi você?</strong> Fale com o nosso suporte agora."),
    )
    return MensagemEmail(
        para=para,
        assunto="O e-mail da sua conta Ledgr foi trocado",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )


def mensagem_cupom(
    *,
    para: str,
    nome: str,
    razao_social: str,
    desconto: str,
    codigo: str | None,
    validade: str | None,
    link_assinatura: str | None,
    chave_idempotencia: str | None = None,
) -> MensagemEmail:
    """Desconto dado pela equipe do Ledgr à conta (issue #169). Com `codigo`, o
    cliente o digita na página de pagamento até a `validade` (já formatada);
    sem ele, o desconto foi aplicado direto na assinatura e nada precisa ser
    feito. O código é de uso único e só serve a esta conta."""
    onde = f" ({link_assinatura})" if link_assinatura else ""
    onde_html = (
        f' (<a href="{escape(link_assinatura, quote=True)}" '
        f'style="color:{_TEXTO};text-decoration:underline;">abrir</a>)'
        if link_assinatura
        else ""
    )
    if codigo:
        corpo = (
            f"A equipe da Ledgr liberou um desconto para a conta da empresa {razao_social}: "
            f"{desconto}.\n\n"
            f"Para usar, abra a tela Assinatura{onde}, escolha o plano e, na página de "
            "pagamento, informe o código:\n\n"
            f"{codigo}\n\n"
            f"O código vale até {validade}, só funciona nesta conta e pode ser usado uma vez."
        )
        corpo_html = (
            _p(
                "A equipe da Ledgr liberou um desconto para a conta da empresa "
                f"<strong>{escape(razao_social)}</strong>: {escape(desconto)}."
            )
            + _p(
                f"Para usar, abra a tela Assinatura{onde_html}, escolha o plano e, na página "
                "de pagamento, informe o código:"
            )
            + '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px;">'
            f'<tr><td style="background:{_FUNDO};border:1px solid {_LINHA};border-radius:10px;'
            "padding:12px 20px;font-family:'Courier New',monospace;font-size:18px;"
            f'letter-spacing:0.08em;color:{_TEXTO};"><strong>{escape(codigo)}</strong></td></tr>'
            "</table>"
            + _nota(
                f"O código vale até {escape(validade or '')}, só funciona nesta conta e "
                "pode ser usado uma vez."
            )
        )
    else:
        corpo = (
            f"A equipe da Ledgr aplicou um desconto na assinatura da empresa {razao_social}: "
            f"{desconto}.\n\n"
            f"Ele aparece nas próximas faturas, e você não precisa fazer nada. Os detalhes "
            f"ficam na tela Assinatura{onde}."
        )
        corpo_html = _p(
            "A equipe da Ledgr aplicou um desconto na assinatura da empresa "
            f"<strong>{escape(razao_social)}</strong>: {escape(desconto)}."
        ) + _p(
            "Ele aparece nas próximas faturas, e você não precisa fazer nada. Os detalhes "
            f"ficam na tela Assinatura{onde_html}."
        )
    texto = f"Olá, {nome}.\n\n{corpo}\n\nEquipe Ledgr"
    html = _pagina(
        "Um desconto para a sua conta",
        "joinha",
        _p(f"Olá, {escape(nome)}.") + corpo_html,
    )
    return MensagemEmail(
        para=para,
        assunto="Um desconto para a sua conta Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )
