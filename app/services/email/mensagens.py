"""Textos dos e-mails transacionais (issues #66, #68, #69, #78, #149 e #169, ADR-012).

Cada mensagem sai em texto puro e em HTML de e-mail (tabelas e estilo inline,
porque os clientes de e-mail ignoram classes), sem rastreamento. O visual é o
do guia de estilo v5 do Ledgr (issue #204): `_pagina` é o molde único, definido
no front em `docs/superpowers/specs/2026-11-12-emails-estilo-v5.md`, e cada
mensagem só entrega o olho, o título, o mascote e o miolo. O logo, o mascote e
as fontes são servidos pelo front, e os nomes são contrato com ele:
`<FRONTEND_URL>/email/{logo,envelope,joinha,comemorando,neutro,dupla}.png` e
`<FRONTEND_URL>/email/fontes/{inter,cormorant-garamond-italico,jetbrains-mono}.woff2`.
Sem `FRONTEND_URL` que sirva, o e-mail sai sem imagem e sem `@font-face`, só
com as fontes de reserva. Tudo o que vem do usuário (nome, razão social) é
escapado no HTML; o link é montado pelo backend, a partir de `FRONTEND_URL` e
de um token URL-safe.
"""

from html import escape

from app.services.email.base import MensagemEmail

# Valores do guia de estilo v5 (app/globals.css do front).
_FUNDO = "#f3f2f2"  # papel
_FOLHA = "#fdfcfa"  # folha do banco: o cartão
_FOLHA_SISTEMA = "#ebe8e3"  # o fundo do código do desconto
_TEXTO = "#201f1d"
_APOIO = "#444141"  # neutro-800
_SUAVE = "#605d5d"  # neutro-700, passa 4,5:1 no papel
_FILETE = "#d3d2d1"  # texto a 16% sobre o papel, em cor sólida (Outlook não faz rgba)
_DOURADO = "#b68235"  # só traço: borda do botão, filete do olho
_DOURADO_700 = "#7d5411"  # todo texto dourado
_TERRACOTA_700 = "#8a4934"  # o rótulo "Não foi você?"
# Apple Mail, iPhone, Outlook no Mac e Samsung carregam a fonte do site; Gmail e
# Outlook no Windows caem na reserva: Arial, Georgia itálico e Courier.
_FONTE = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
_FONTE_DESTAQUE = "'Cormorant Garamond', Georgia, 'Times New Roman', serif"
_FONTE_MONO = "'JetBrains Mono', 'Courier New', monospace"
_MONO_ROTULO = (
    f"font-family:{_FONTE_MONO};font-size:10px;font-weight:500;"
    "letter-spacing:0.16em;text-transform:uppercase;"
)


def _fontes(base: str | None) -> str:
    """@font-face com as fontes servidas pelo front; sem base, nada (vale a reserva)."""
    if not base:
        return ""
    url = escape(f"{base}/email/fontes", quote=True)
    return (
        "<style>"
        "@font-face{font-family:'Inter';font-weight:400 700;"
        f"src:url('{url}/inter.woff2') format('woff2');}}"
        "@font-face{font-family:'Cormorant Garamond';font-style:italic;font-weight:500;"
        f"src:url('{url}/cormorant-garamond-italico.woff2') format('woff2');}}"
        "@font-face{font-family:'JetBrains Mono';font-weight:500;"
        f"src:url('{url}/jetbrains-mono.woff2') format('woff2');}}"
        "</style>"
    )


def _p(conteudo_html: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;">{conteudo_html}</p>'


def _link(rotulo: str, url: str) -> str:
    """Link no meio do texto: tinta do texto, sublinhado dourado."""
    return (
        f'<a href="{escape(url, quote=True)}" style="color:{_TEXTO};text-decoration:underline;'
        f'text-decoration-color:{_DOURADO};">{escape(rotulo)}</a>'
    )


def _botao(rotulo: str, url: str) -> str:
    """Contornado como o `.btn-primary` do app, com a seta de quem leva adiante."""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 22px;">'
        f'<tr><td style="border:1px solid {_DOURADO};border-radius:16px;">'
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:11px 24px;'
        f"font-family:{_FONTE};font-size:14px;font-weight:600;color:{_DOURADO_700};"
        f'text-decoration:none;">{escape(rotulo)} &rarr;</a></td></tr></table>'
    )


def _ficha(*linhas: tuple[str, str], mono: bool = False) -> str:
    """Prazos e dados do pedido como linhas de um extrato: rótulo em mono, valor à direita."""
    valor = f"font-family:{_FONTE_MONO};font-size:13px;" if mono else "font-size:14px;"
    corpo = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid {_FILETE};{_MONO_ROTULO}'
        f'color:{_SUAVE};">{escape(rotulo)}</td>'
        f'<td align="right" style="padding:10px 0 10px 16px;border-bottom:1px solid {_FILETE};'
        f'{valor}font-weight:500;color:{_TEXTO};">{escape(texto)}</td></tr>'
        for rotulo, texto in linhas
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 18px;border-top:1px solid {_FILETE};">{corpo}</table>'
    )


def _endereco(url: str) -> str:
    """O link por extenso, para quem não consegue clicar no botão."""
    return (
        f'<p style="margin:0 0 22px;font-size:13px;line-height:1.5;color:{_SUAVE};">'
        "Se o botão não funcionar, copie e cole este endereço no navegador:<br>"
        f'<span style="font-family:{_FONTE_MONO};font-size:11.5px;color:{_TEXTO};'
        f'word-break:break-all;">{escape(url, quote=True)}</span></p>'
    )


def _nota(conteudo_html: str) -> str:
    return (
        f'<p style="margin:0;border-top:1px solid {_FILETE};padding-top:16px;font-size:13.5px;'
        f'line-height:1.55;color:{_SUAVE};">{conteudo_html}</p>'
    )


def _aviso(conteudo_html: str) -> str:
    """O "Não foi você?" que fecha os e-mails de conta: rótulo em terracota, texto em tinta."""
    return (
        f'<div style="margin:6px 0 0;border-top:1px solid {_FILETE};padding-top:16px;'
        f'font-size:14px;line-height:1.55;color:{_APOIO};">'
        f'<div style="margin:0 0 4px;{_MONO_ROTULO}color:{_TERRACOTA_700};">Não foi você?</div>'
        f"{conteudo_html}</div>"
    )


def _codigo(codigo: str) -> str:
    """O código do desconto, em mono, na caixa tracejada."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:2px 0 22px;">'
        f'<tr><td align="center" style="background:{_FOLHA_SISTEMA};border:1px dashed {_DOURADO};'
        'border-radius:8px;padding:16px 20px;">'
        f'<div style="margin:0 0 6px;{_MONO_ROTULO}color:{_DOURADO_700};">Código</div>'
        f'<div style="font-family:{_FONTE_MONO};font-size:22px;font-weight:500;'
        f'letter-spacing:0.14em;color:{_TEXTO};">{escape(codigo)}</div></td></tr></table>'
    )


def _pagina(olho: str, titulo: str, destaque: str, mascote: str, corpo: str) -> str:
    """Molde de todos os e-mails: assinatura no alto, folha com olho, título em duas
    vozes e mascote, e o rodapé. `olho`, `titulo` e `destaque` são texto fixo (o
    destaque é o fim do título, em itálico); `mascote` é o nome do PNG em
    `<FRONTEND_URL>/email/` (envelope, joinha, comemorando, neutro, dupla)."""
    # Import preguiçoso, como em app/services/email/__init__.py: importar este
    # módulo não pode exigir a configuração.
    from app.core.frontend_url import frontend_url_para_link

    base = frontend_url_para_link()
    imagens = escape(f"{base}/email", quote=True) if base else None
    site = escape(base, quote=True) if base else None

    logo = (
        f'<td style="vertical-align:middle;padding-right:10px;"><img src="{imagens}/logo.png" '
        'width="32" alt="" style="display:block;border:0;height:auto;"></td>'
        if imagens
        else ""
    )
    figura = (
        '<td width="112" align="right" style="vertical-align:top;padding-left:16px;">'
        f'<img src="{imagens}/{escape(mascote, quote=True)}.png" width="96" alt="" '
        'style="display:block;border:0;height:auto;max-width:96px;"></td>'
        if imagens
        else ""
    )
    # o endereço escrito é o do próprio site (ledgrfinance.com.br em produção)
    endereco_do_site = (
        f' · <a href="{site}" style="color:{_DOURADO_700};text-decoration:underline;">'
        f'{escape(base.split("//", 1)[-1].rstrip("/"))}</a>'
        if base
        else ""
    )
    return (
        _fontes(base) + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{_FUNDO}" style="background:{_FUNDO};font-family:{_FONTE};color:{_TEXTO};">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="576" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:576px;">'
        # assinatura: logo de adesivo, nome e descritor separados por filete
        '<tr><td style="padding:0 4px 18px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>{logo}'
        '<td style="vertical-align:middle;font-size:20px;font-weight:600;letter-spacing:-0.01em;'
        f'color:{_TEXTO};">Ledgr</td>'
        '<td style="vertical-align:middle;padding-left:11px;">'
        f'<div style="border-left:1px solid {_FILETE};'
        "padding-left:11px;font-size:10px;font-weight:500;letter-spacing:0.18em;"
        f'text-transform:uppercase;color:{_SUAVE};">Conciliação bancária</div></td>'
        "</tr></table></td></tr>"
        # a folha
        f'<tr><td bgcolor="{_FOLHA}" style="background:{_FOLHA};border:1px solid {_FILETE};'
        'border-radius:16px;padding:36px 36px 30px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 22px;">'
        '<tr><td style="vertical-align:top;">'
        f'<div style="margin:0 0 12px;{_MONO_ROTULO}font-size:11px;letter-spacing:0.2em;'
        f'color:{_DOURADO_700};">{escape(olho)}'
        f'<span style="display:inline-block;width:32px;height:1px;background:{_DOURADO};'
        'vertical-align:middle;margin-left:10px;"></span></div>'
        f'<h1 style="margin:0;font-family:{_FONTE};font-size:30px;line-height:1.12;'
        f'font-weight:500;letter-spacing:-0.02em;color:{_TEXTO};">{escape(titulo)} '
        f'<em style="font-family:{_FONTE_DESTAQUE};font-style:italic;font-weight:500;'
        f'font-size:1.2em;letter-spacing:-0.01em;">{escape(destaque)}</em></h1></td>'
        f"{figura}</tr></table>"
        f"{corpo}</td></tr>"
        # rodapé com a frase do guia
        f'<tr><td style="padding:18px 4px 0;font-size:12px;line-height:1.55;color:{_SUAVE};">'
        f'<div style="margin:0 0 4px;font-family:{_FONTE_DESTAQUE};font-style:italic;'
        f'font-weight:500;font-size:16px;color:{_APOIO};">'
        "O extrato do banco é sempre a fonte da verdade.</div>"
        f"Equipe Ledgr · Conciliação bancária{endereco_do_site}</td></tr>"
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
        olho="Acesso · senha",
        titulo="Redefinir sua",
        destaque="senha.",
        mascote="envelope",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _p(
            "Recebemos um pedido para redefinir a senha da sua conta na Ledgr. "
            "Para criar uma senha nova, use o botão abaixo."
        )
        + _botao("Redefinir minha senha", link)
        + _ficha(("Vale por", f"{validade_minutos} minutos"), ("Uso", "Uma vez só"))
        + _endereco(link)
        + _aviso("Ignore este e-mail: sua senha atual continua valendo."),
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
        f" na {_link('tela de login', link_login)}" if link_login else " na tela de login"
    )
    html = _pagina(
        olho="Acesso · senha",
        titulo="Sua senha foi",
        destaque="alterada.",
        mascote="joinha",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _p(
            "A senha da sua conta na Ledgr acabou de ser alterada. Se foi você, não precisa fazer nada."
        )
        + _aviso(
            "Alguém pode ter acesso à sua conta. Use o "
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
        f" na {_link('tela de login', link_login)}" if link_login else " na tela de login"
    )
    html = _pagina(
        olho="Cadastro · conta nova",
        titulo="Sua conta está",
        destaque="pronta.",
        mascote="comemorando",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _ficha(("Empresa", razao_social))
        + _p(
            "Sua conta na Ledgr foi criada para essa empresa. "
            f"Para começar, entre{onde_entrar_html} e envie o extrato do banco e o "
            "extrato do seu sistema de gestão do mesmo período. A Ledgr cruza os dois e "
            "mostra o que bate e o que precisa da sua atenção."
        )
        + (_botao("Entrar na Ledgr", link_login) if link_login else "")
        + _aviso(
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
        olho="Equipe · convite",
        titulo="Você foi convidado para a",
        destaque="Ledgr.",
        mascote="dupla",
        corpo=_p("Olá.")
        + _p(
            f"<strong>{escape(nome_de_quem_convidou)}</strong> convidou você para usar a "
            f"Ledgr na empresa <strong>{escape(razao_social)}</strong>."
        )
        + _p("Para aceitar, use o botão abaixo e crie a sua senha.")
        + _botao("Aceitar o convite", link)
        + _ficha(("Vale por", f"{validade_dias} dias"), ("Uso", "Uma vez só"))
        + _endereco(link)
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
    corpo_html += _aviso("Avise o nosso suporte agora.")
    return MensagemEmail(
        para=para,
        assunto="Sua conta da Ledgr foi desativada",
        texto=texto,
        html=_pagina(
            olho="Conta · desativada",
            titulo="Sua conta foi",
            destaque="desativada.",
            mascote="neutro",
            corpo=corpo_html,
        ),
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
        olho="Conta · e-mail novo",
        titulo="Confirme o seu",
        destaque="e-mail novo.",
        mascote="envelope",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _p(
            "Recebemos um pedido para usar este e-mail na sua conta da Ledgr. Para "
            "confirmar a troca, use o botão abaixo."
        )
        + _botao("Confirmar o e-mail novo", link)
        + _ficha(("Vale por", f"{validade_minutos} minutos"), ("Uso", "Uma vez só"))
        + _p("Depois da confirmação, você entra na Ledgr com este e-mail.")
        + _endereco(link)
        + _aviso("Ignore este e-mail: nada muda na conta."),
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
        olho="Conta · e-mail",
        titulo="Pediram para trocar o seu",
        destaque="e-mail.",
        mascote="envelope",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _p(
            "Pediram a troca do e-mail da sua conta na Ledgr para o endereço abaixo. "
            "A troca só vale depois que alguém confirmar pelo link enviado ao e-mail novo."
        )
        + _ficha(("E-mail novo", mascarado), mono=True)
        + _p("Se foi você, não precisa fazer nada aqui.")
        + _aviso("Alguém sabe a sua senha: troque a senha agora e fale com o nosso suporte."),
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
        olho="Conta · e-mail",
        titulo="Seu e-mail foi",
        destaque="trocado.",
        mascote="joinha",
        corpo=_p(f"Olá, {escape(nome)}.")
        + _p(
            "O e-mail da sua conta na Ledgr foi trocado para o endereço abaixo. A partir de "
            "agora, o login é com o e-mail novo, e as sessões abertas foram encerradas."
        )
        + _ficha(("E-mail novo", mascarado), mono=True)
        + _p("Se foi você, não precisa fazer nada.")
        + _aviso("Fale com o nosso suporte agora."),
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
    tela_html = _link("Assinatura", link_assinatura) if link_assinatura else "Assinatura"
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
                f"<strong>{escape(razao_social)}</strong>."
            )
            + _ficha(
                ("Desconto", desconto),
                *([("Vale até", validade)] if validade else []),
                ("Uso", "Uma vez só, nesta conta"),
            )
            + _p(
                f"Para usar, abra a tela {tela_html}, escolha o plano e, na página "
                "de pagamento, informe o código:"
            )
            + _codigo(codigo)
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
            f"ficam na tela {tela_html}."
        )
    texto = f"Olá, {nome}.\n\n{corpo}\n\nEquipe Ledgr"
    html = _pagina(
        olho="Assinatura · desconto",
        titulo="Um desconto para a sua",
        destaque="conta.",
        mascote="joinha",
        corpo=_p(f"Olá, {escape(nome)}.") + corpo_html,
    )
    return MensagemEmail(
        para=para,
        assunto="Um desconto para a sua conta Ledgr",
        texto=texto,
        html=html,
        chave_idempotencia=chave_idempotencia,
    )
