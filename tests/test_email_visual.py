"""Molde visual dos e-mails transacionais (issues #188 e #204, estilo v5).

Todas as mensagens passam por `_pagina`. Nada aqui compara o HTML bruto com um
retrato: o que se confere é o que o molde não pode quebrar. O texto puro e o
assunto não mudam; o link do botão e os dados do usuário saem escapados; o
olho, o título e o mascote são os de cada e-mail; prazos, empresa e e-mail
mascarado ficam na ficha; o logo, o mascote e as fontes só aparecem quando
`FRONTEND_URL` serve, e só com os nomes que o front publica em `/email/`.
"""

import json
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from casos_email import BASE, CASOS, CODIGO, LINK, RAZAO_SOCIAL, TODOS
from conftest import FRONTEND_URL_TESTE

from app.core.config import settings
from app.services.email import mensagens as m

# Contrato com o front (public/email/, guardado lá por app/email-publico.test.ts):
# renomear qualquer um destes, de um lado só, quebra os e-mails.
IMAGENS_DO_CONTRATO = {
    "logo.png",
    "envelope.png",
    "joinha.png",
    "comemorando.png",
    "neutro.png",
    "dupla.png",
}
FONTES_DO_CONTRATO = {
    "fontes/inter.woff2",
    "fontes/cormorant-garamond-italico.woff2",
    "fontes/jetbrains-mono.woff2",
}
FRASE_DO_RODAPE = "O extrato do banco é sempre a fonte da verdade."
MASCARADO = "n***@telhacerta.com.br"
VALE_60_MINUTOS = [("Vale por", "60 minutos"), ("Uso", "Uma vez só")]

# A tabela "Cada e-mail" do documento do front, como ficou no backend.
ESPERADO = {
    "recuperacao_senha": {
        "olho": "Acesso · senha",
        "titulo": ("Redefinir sua", "senha."),
        "mascote": "envelope",
        "botao": ("Redefinir minha senha", LINK),
        "ficha": VALE_60_MINUTOS,
    },
    "senha_alterada": {
        "olho": "Acesso · senha",
        "titulo": ("Sua senha foi", "alterada."),
        "mascote": "joinha",
    },
    "boas_vindas": {
        "olho": "Cadastro · conta nova",
        "titulo": ("Sua conta está", "pronta."),
        "mascote": "comemorando",
        "botao": ("Entrar na Ledgr", f"{BASE}/login"),
        "ficha": [("Empresa", RAZAO_SOCIAL)],
    },
    "convite": {
        "olho": "Equipe · convite",
        "titulo": ("Você foi convidado para a", "Ledgr."),
        "mascote": "dupla",
        "botao": ("Aceitar o convite", LINK),
        "ficha": [("Vale por", "7 dias"), ("Uso", "Uma vez só")],
    },
    "conta_desativada": {
        "olho": "Conta · desativada",
        "titulo": ("Sua conta foi", "desativada."),
        "mascote": "neutro",
    },
    "confirmacao_troca_email": {
        "olho": "Conta · e-mail novo",
        "titulo": ("Confirme o seu", "e-mail novo."),
        "mascote": "envelope",
        "botao": ("Confirmar o e-mail novo", LINK),
        "ficha": VALE_60_MINUTOS,
    },
    "troca_email_pedida": {
        "olho": "Conta · e-mail",
        "titulo": ("Pediram para trocar o seu", "e-mail."),
        "mascote": "envelope",
        "ficha": [("E-mail novo", MASCARADO)],
    },
    "troca_email_concluida": {
        "olho": "Conta · e-mail",
        "titulo": ("Seu e-mail foi", "trocado."),
        "mascote": "joinha",
        "ficha": [("E-mail novo", MASCARADO)],
    },
    "cupom": {
        "olho": "Assinatura · desconto",
        "titulo": ("Um desconto para a sua", "conta."),
        "mascote": "joinha",
        "ficha": [
            ("Desconto", "20% por 3 meses"),
            ("Vale até", "30/11/2026"),
            ("Uso", "Uma vez só, nesta conta"),
        ],
    },
}
# Quem leva o nome da empresa no HTML.
COM_RAZAO_SOCIAL = {"boas_vindas", "convite", "conta_desativada", "cupom"}


class _Linhas(HTMLParser):
    """Lê as linhas das tabelas do e-mail sem depender do estilo: cada `<tr>` que
    não contém outra tabela vira a lista das suas células, e cada célula, a lista
    dos trechos de texto dentro dela."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.linhas: list[list[list[str]]] = []
        self._pilha: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and self._pilha:
            self._pilha[-1]["tem_tabela"] = True
        elif tag == "tr":
            self._pilha.append({"celulas": [], "tem_tabela": False})
        elif tag == "td" and self._pilha:
            self._pilha[-1]["celulas"].append([])

    def handle_endtag(self, tag):
        if tag == "tr" and self._pilha:
            linha = self._pilha.pop()
            if not linha["tem_tabela"]:
                self.linhas.append(linha["celulas"])

    def handle_data(self, data):
        if self._pilha and self._pilha[-1]["celulas"] and data.strip():
            self._pilha[-1]["celulas"][-1].append(data.strip())


def _linhas(html: str) -> list[list[list[str]]]:
    leitor = _Linhas()
    leitor.feed(html)
    return leitor.linhas


def _ficha(html: str) -> list[tuple[str, str]]:
    """As linhas da ficha: rótulo numa célula, valor na outra."""
    return [
        (linha[0][0], linha[1][0])
        for linha in _linhas(html)
        if len(linha) == 2 and len(linha[0]) == 1 and len(linha[1]) == 1
    ]


def _arquivos_do_front(html: str) -> set[str]:
    """Tudo o que o HTML pede em `<FRONTEND_URL>/email/`, pelo nome."""
    return set(re.findall(re.escape(f"{FRONTEND_URL_TESTE}/email/") + r"([^\"')\s]+)", html))


@pytest.fixture
def frontend_valida(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", FRONTEND_URL_TESTE)


def test_os_casos_cobrem_os_nove_emails():
    funcoes = {nome for nome in dir(m) if nome.startswith("mensagem_")}

    assert {f"mensagem_{nome}" for nome in CASOS} == funcoes
    assert set(ESPERADO) == set(CASOS)
    assert BASE == FRONTEND_URL_TESTE


# --- texto e assunto fixados (issue #204) ---------------------------------------------

TEXTOS_FIXADOS = json.loads(
    (Path(__file__).parent / "fixtures" / "emails_texto.json").read_text(encoding="utf-8")
)


def test_o_arquivo_de_textos_cobre_todos_os_casos():
    assert set(TEXTOS_FIXADOS) == set(TODOS)


@pytest.mark.parametrize("nome", TODOS)
def test_texto_e_assunto_nao_mudam(nome):
    """O texto puro e o assunto foram gravados antes do molde v5: mexer no visual
    do HTML não pode mudar nenhum dos dois."""
    mensagem = TODOS[nome]()

    assert mensagem.assunto == TEXTOS_FIXADOS[nome]["assunto"]
    assert mensagem.texto == TEXTOS_FIXADOS[nome]["texto"]


# --- o molde em cada e-mail -----------------------------------------------------------


@pytest.mark.parametrize("nome", CASOS)
def test_olho_titulo_em_duas_vozes_e_rodape(nome, frontend_valida):
    html = CASOS[nome]().html
    titulo, destaque = ESPERADO[nome]["titulo"]

    assert f">{ESPERADO[nome]['olho']}<" in html
    assert re.search(
        rf"<h1[^>]*>{re.escape(titulo)} <em[^>]*>{re.escape(destaque)}</em></h1>", html
    )
    assert html.count("<h1") == 1
    assert FRASE_DO_RODAPE in html
    assert f'<a href="{FRONTEND_URL_TESTE}"' in html
    assert "<script" not in html


@pytest.mark.parametrize("nome", CASOS)
def test_o_email_leva_o_logo_e_o_mascote_certo(nome, frontend_valida):
    html = CASOS[nome]().html

    assert re.findall(r'<img src="([^"]+)"', html) == [
        f"{FRONTEND_URL_TESTE}/email/logo.png",
        f"{FRONTEND_URL_TESTE}/email/{ESPERADO[nome]['mascote']}.png",
    ]


@pytest.mark.parametrize("nome", [n for n in CASOS if "botao" in ESPERADO[n]])
def test_o_link_do_botao_sai_escapado(nome, frontend_valida):
    html = CASOS[nome]().html
    rotulo, url = ESPERADO[nome]["botao"]

    assert re.search(
        rf'<a href="{re.escape(escape(url, quote=True))}"[^>]*>{re.escape(rotulo)} &rarr;</a>',
        html,
    )
    if "&" in url:
        assert url not in html


@pytest.mark.parametrize("nome", [n for n in CASOS if "ficha" in ESPERADO[n]])
def test_prazos_empresa_e_email_mascarado_ficam_na_ficha(nome, frontend_valida):
    html = CASOS[nome]().html
    ficha = _ficha(html)

    for linha in ESPERADO[nome]["ficha"]:
        assert linha in ficha


@pytest.mark.parametrize("nome", ["troca_email_pedida", "troca_email_concluida"])
def test_o_email_mascarado_sai_da_frase(nome, frontend_valida):
    html = CASOS[nome]().html

    assert html.count(MASCARADO) == 1
    assert "para o endereço abaixo." in html


def test_a_empresa_das_boas_vindas_sai_da_frase(frontend_valida):
    html = CASOS["boas_vindas"]().html

    assert html.count(RAZAO_SOCIAL) == 1
    assert "Sua conta na Ledgr foi criada para essa empresa." in html


def test_o_codigo_do_cupom_fica_no_bloco_de_codigo(frontend_valida):
    html = CASOS["cupom"]().html

    assert [["Código", CODIGO]] in _linhas(html)
    assert html.count(CODIGO) == 1


def test_cupom_sem_codigo_nao_tem_bloco_de_codigo_nem_ficha(frontend_valida):
    html = TODOS["cupom_sem_codigo"]().html

    assert "Código" not in html
    assert _ficha(html) == []
    assert f'<a href="{FRONTEND_URL_TESTE}/assinatura"' in html
    assert ">Assinatura</a>" in html


@pytest.mark.parametrize("nome", ["senha_alterada", "conta_desativada", "troca_email_pedida"])
def test_o_aviso_leva_o_nao_foi_voce_uma_vez_como_rotulo(nome, frontend_valida):
    html = CASOS[nome]().html

    assert html.count("Não foi você?") == 1
    assert "<strong>Não foi você?</strong>" not in html


# --- escape ---------------------------------------------------------------------------

NOME_HOSTIL = "<script>alert(1)</script> & Cia"
RAZAO_HOSTIL = '<script>x</script> & "Filhos"'


@pytest.mark.parametrize("nome", CASOS)
def test_nome_e_razao_social_saem_escapados(nome, frontend_valida):
    html = CASOS[nome](nome=NOME_HOSTIL, razao=RAZAO_HOSTIL).html

    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Cia" in html
    if nome in COM_RAZAO_SOCIAL:
        assert "&lt;script&gt;x&lt;/script&gt; &amp; &quot;Filhos&quot;" in html


def test_os_ajudantes_escapam_o_que_recebem(frontend_valida):
    hostil = '<b>"x" & y</b>'
    escapado = "&lt;b&gt;&quot;x&quot; &amp; y&lt;/b&gt;"

    for html in (
        m._ficha((hostil, hostil)),
        m._codigo(hostil),
        m._botao(hostil, f"https://x.com/?a={hostil}"),
        m._link(hostil, f"https://x.com/?a={hostil}"),
        m._endereco(f"https://x.com/?a={hostil}"),
        m._pagina(olho=hostil, titulo=hostil, destaque=hostil, mascote=hostil, corpo=""),
    ):
        assert "<b>" not in html
        assert escapado in html


# --- imagens e fontes do front --------------------------------------------------------


def test_todo_arquivo_pedido_ao_front_esta_no_contrato(frontend_valida):
    """Pega erro de digitação no nome de uma imagem ou de uma fonte, e também um
    nome do contrato que deixou de ser usado."""
    pedidos = set()
    for nome in TODOS:
        pedidos |= _arquivos_do_front(TODOS[nome]().html)

    assert pedidos == IMAGENS_DO_CONTRATO | FONTES_DO_CONTRATO


@pytest.mark.parametrize("nome", CASOS)
def test_as_fontes_vem_do_front_e_a_reserva_fecha_a_pilha(nome, frontend_valida):
    html = CASOS[nome]().html

    assert html.count("@font-face") == 3
    assert FONTES_DO_CONTRATO <= _arquivos_do_front(html)
    # Gmail e Outlook no Windows não carregam fonte web: caem na reserva.
    assert "Arial, sans-serif" in html
    assert "'Cormorant Garamond', Georgia," in html and "font-style:italic" in html
    assert "'JetBrains Mono', 'Courier New', monospace" in html


@pytest.mark.parametrize("nome", CASOS)
def test_nenhum_endereco_de_terceiro(nome, frontend_valida):
    html = CASOS[nome]().html

    enderecos = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    enderecos += re.findall(r"url\('([^']+)'\)", html)
    assert enderecos
    assert all(e == FRONTEND_URL_TESTE or e.startswith(f"{FRONTEND_URL_TESTE}/") for e in enderecos)


@pytest.mark.parametrize("frontend_url", ["", "https://frontend-hfrnofw9w-ledgr7.vercel.app"])
@pytest.mark.parametrize("nome", TODOS)
def test_sem_frontend_url_que_sirva_o_email_sai_sem_imagem_e_sem_fonte(
    nome, frontend_url, monkeypatch
):
    monkeypatch.setattr(settings, "frontend_url", frontend_url)

    html = TODOS[nome]().html

    assert "<img" not in html
    assert "@font-face" not in html and "<style" not in html and "woff2" not in html
    assert "/email/" not in html
    assert "vercel.app" not in html
    # o e-mail continua montando: título, corpo e rodapé
    assert "<h1" in html
    assert "Olá" in html
    assert FRASE_DO_RODAPE in html
    assert "Arial, sans-serif" in html
