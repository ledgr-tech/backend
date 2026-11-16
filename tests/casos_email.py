"""Os e-mails transacionais montados com dados fixos, para os testes do molde.

`CASOS` tem um caso por mensagem de `app/services/email/mensagens.py` (os 9
e-mails); `VARIANTES` cobre os ramos que mudam o texto: sem link, com a
empresa sem acesso, cupom sem código. O texto e o assunto de todos estão
fixados em `tests/fixtures/emails_texto.json`.
"""

from app.services.email import mensagens as m

BASE = "https://app.ledgr.com.br"  # o mesmo valor de conftest.FRONTEND_URL_TESTE
LINK = f"{BASE}/redefinir-senha#token=abc&v=1"
NOME = "Ana"
RAZAO_SOCIAL = "Telha Certa Ltda"
EMAIL_NOVO = "novo@telhacerta.com.br"
CODIGO = "AURORA-7K2M"

CASOS = {
    "recuperacao_senha": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_recuperacao_senha(
        para="a@x.com", nome=nome, link=LINK, validade_minutos=60
    ),
    "senha_alterada": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_senha_alterada(
        para="a@x.com", nome=nome, link_login=f"{BASE}/login"
    ),
    "boas_vindas": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_boas_vindas(
        para="a@x.com", nome=nome, razao_social=razao, link_login=f"{BASE}/login"
    ),
    "convite": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_convite(
        para="a@x.com",
        nome_de_quem_convidou=nome,
        razao_social=razao,
        link=LINK,
        validade_dias=7,
    ),
    "conta_desativada": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_conta_desativada(
        para="a@x.com", nome=nome, razao_social_sem_acesso=razao
    ),
    "confirmacao_troca_email": lambda nome=NOME, razao=RAZAO_SOCIAL: (
        m.mensagem_confirmacao_troca_email(
            para="a@x.com", nome=nome, link=LINK, validade_minutos=60
        )
    ),
    "troca_email_pedida": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_troca_email_pedida(
        para="a@x.com", nome=nome, email_novo=EMAIL_NOVO
    ),
    "troca_email_concluida": lambda nome=NOME, razao=RAZAO_SOCIAL: (
        m.mensagem_troca_email_concluida(para="a@x.com", nome=nome, email_novo=EMAIL_NOVO)
    ),
    "cupom": lambda nome=NOME, razao=RAZAO_SOCIAL: m.mensagem_cupom(
        para="a@x.com",
        nome=nome,
        razao_social=razao,
        desconto="20% por 3 meses",
        codigo=CODIGO,
        validade="30/11/2026",
        link_assinatura=f"{BASE}/assinatura",
    ),
}

VARIANTES = {
    "senha_alterada_sem_link": lambda: m.mensagem_senha_alterada(
        para="a@x.com", nome=NOME, link_login=None
    ),
    "boas_vindas_sem_link": lambda: m.mensagem_boas_vindas(
        para="a@x.com", nome=NOME, razao_social=RAZAO_SOCIAL, link_login=None
    ),
    "conta_desativada_sem_empresa": lambda: m.mensagem_conta_desativada(
        para="a@x.com", nome=NOME, razao_social_sem_acesso=None
    ),
    "cupom_com_codigo_sem_link": lambda: m.mensagem_cupom(
        para="a@x.com",
        nome=NOME,
        razao_social=RAZAO_SOCIAL,
        desconto="20% por 3 meses",
        codigo=CODIGO,
        validade="30/11/2026",
        link_assinatura=None,
    ),
    "cupom_sem_codigo": lambda: m.mensagem_cupom(
        para="a@x.com",
        nome=NOME,
        razao_social=RAZAO_SOCIAL,
        desconto="20% por 3 meses",
        codigo=None,
        validade=None,
        link_assinatura=f"{BASE}/assinatura",
    ),
    "cupom_sem_codigo_sem_link": lambda: m.mensagem_cupom(
        para="a@x.com",
        nome=NOME,
        razao_social=RAZAO_SOCIAL,
        desconto="20% por 3 meses",
        codigo=None,
        validade=None,
        link_assinatura=None,
    ),
}

TODOS = {**CASOS, **VARIANTES}
