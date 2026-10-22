"""Textos dos e-mails transacionais (issues #66, #68, #69, #78 e #149, ADR-012).

Cada mensagem sai em texto puro e em HTML simples, sem imagem nem
rastreamento. Tudo o que vem do usuário (nome, razão social) é escapado no
HTML; o link é montado pelo backend, a partir de `FRONTEND_URL` e de um token
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
        f' na <a href="{escape(link_login, quote=True)}">tela de login</a>'
        if link_login
        else " na tela de login"
    )
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        f"<p>Sua conta na Ledgr foi criada para a empresa {escape(razao_social)}.</p>"
        f"<p>Para começar, entre{onde_entrar_html} e envie o extrato do banco e o "
        "extrato do seu sistema de gestão do mesmo período. A Ledgr cruza os dois e "
        "mostra o que bate e o que precisa da sua atenção.</p>"
        "<p>Se não foi você quem criou esta conta, ignore este e-mail e avise o nosso "
        "suporte.</p>"
        "<p>Equipe Ledgr</p>"
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
    link_html = escape(link, quote=True)
    html = (
        "<p>Olá.</p>"
        f"<p>{escape(nome_de_quem_convidou)} convidou você para usar a Ledgr na empresa "
        f"{escape(razao_social)}.</p>"
        "<p>Para aceitar, use o botão abaixo e crie a sua senha:</p>"
        f'<p><a href="{link_html}">Aceitar o convite</a></p>'
        f"<p>Se o botão não funcionar, copie e cole este endereço no navegador:<br>"
        f"{link_html}</p>"
        f"<p>O convite vale por {validade_dias} dias e só pode ser usado uma vez.</p>"
        "<p>Se você não esperava este convite, ignore este e-mail: nenhuma conta é "
        "criada sem o aceite.</p>"
        "<p>Equipe Ledgr</p>"
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
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        "<p>A sua conta na Ledgr foi desativada a seu pedido. Ela não entra mais, e "
        "este e-mail fica livre para um cadastro novo.</p>"
    )
    if razao_social_sem_acesso is not None:
        texto += (
            f"Os dados da empresa {razao_social_sem_acesso} continuam guardados, sem "
            "acesso de ninguém. Para pedir a exclusão deles, escreva ao nosso suporte.\n\n"
        )
        html += (
            f"<p>Os dados da empresa {escape(razao_social_sem_acesso)} continuam guardados, "
            "sem acesso de ninguém. Para pedir a exclusão deles, escreva ao nosso "
            "suporte.</p>"
        )
    texto += "Se não foi você, avise o nosso suporte agora.\n\nEquipe Ledgr"
    html += "<p>Se não foi você, avise o nosso suporte agora.</p><p>Equipe Ledgr</p>"
    return MensagemEmail(
        para=para,
        assunto="Sua conta da Ledgr foi desativada",
        texto=texto,
        html=html,
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
    link_html = escape(link, quote=True)
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        "<p>Recebemos um pedido para usar este e-mail na sua conta da Ledgr. Para "
        "confirmar a troca, use o botão abaixo:</p>"
        f'<p><a href="{link_html}">Confirmar o e-mail novo</a></p>'
        f"<p>Se o botão não funcionar, copie e cole este endereço no navegador:<br>"
        f"{link_html}</p>"
        f"<p>O link vale por {validade_minutos} minutos e só pode ser usado uma vez. "
        "Depois da confirmação, você entra na Ledgr com este e-mail.</p>"
        "<p>Se não foi você, ignore este e-mail: nada muda na conta.</p>"
        "<p>Equipe Ledgr</p>"
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
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        f"<p>Pediram a troca do e-mail da sua conta na Ledgr para "
        f"<strong>{escape(mascarado)}</strong>. A troca só vale depois que alguém "
        "confirmar pelo link enviado ao e-mail novo.</p>"
        "<p>Se foi você, não precisa fazer nada aqui.</p>"
        "<p>Se não foi você, alguém sabe a sua senha: troque a senha agora e fale com o "
        "nosso suporte.</p>"
        "<p>Equipe Ledgr</p>"
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
    html = (
        f"<p>Olá, {escape(nome)}.</p>"
        f"<p>O e-mail da sua conta na Ledgr foi trocado para "
        f"<strong>{escape(mascarado)}</strong>. A partir de agora, o login é com o "
        "e-mail novo, e as sessões abertas foram encerradas.</p>"
        "<p>Se foi você, não precisa fazer nada.</p>"
        "<p>Se não foi você, fale com o nosso suporte agora.</p>"
        "<p>Equipe Ledgr</p>"
    )
    return MensagemEmail(
        para=para,
        assunto="O e-mail da sua conta Ledgr foi trocado",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )
