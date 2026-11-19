"""Limite por IP no `/admin/*` (issue #234), atrás de `ADMIN_LIMITE_POR_IP_HABILITADO`.

Soma-se ao limite por usuário, não o substitui. Desligada (o padrão), a rota se
comporta como antes. O IP é o de `ip_do_cliente`: aqui, o último item do
`X-Forwarded-For`, o que o proxy da Railway anota. Os valores por IP são
trocados por números pequenos para o teste ser rápido; o das ações por usuário
(10 por minuto) é o de verdade.
"""

import pytest
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.config import settings
from main import app

client = TestClient(app)

IP_A = "203.0.113.10"
IP_B = "203.0.113.20"


def _com_ip(headers, ip):
    return {**headers, "X-Forwarded-For": ip}


def _ler(headers, ip=IP_A):
    return client.get("/admin/contas/por-uf", headers=_com_ip(headers, ip))


def _agir(headers, conta_id, ip=IP_A):
    # Reativar uma conta ativa dá 409: gasta o limite sem mudar nada.
    return client.post(
        f"/admin/contas/{conta_id}/reativar", headers=_com_ip(headers, ip), json={"motivo": "x"}
    )


@pytest.fixture
def limite_por_ip_pequeno(monkeypatch):
    monkeypatch.setattr(rate_limit, "LIMITE_ADMIN_LEITURA_POR_IP", "3/minute")
    monkeypatch.setattr(rate_limit, "LIMITE_ADMIN_ACAO_POR_IP", "4/minute")


@pytest.fixture
def flag_ligada(monkeypatch):
    monkeypatch.setattr(settings, "admin_limite_por_ip_habilitado", True)


def test_a_flag_nasce_desligada():
    from app.core.config import Settings

    assert (
        Settings(_env_file=None, database_url="postgresql://x/y").admin_limite_por_ip_habilitado
        is False
    )


def test_os_valores_iniciais_sao_120_leituras_e_30_acoes_por_minuto():
    assert rate_limit.LIMITE_ADMIN_LEITURA_POR_IP == "120/minute"
    assert rate_limit.LIMITE_ADMIN_ACAO_POR_IP == "30/minute"


# --- flag desligada ----------------------------------------------------------------------


def test_flag_desligada_nao_limita_por_ip(operador, cliente, limite_por_ip_pequeno):
    assert settings.admin_limite_por_ip_habilitado is False
    um, dois = operador(), operador(nome="Rui Operador")
    c = cliente()

    leituras = [_ler((um, dois)[i % 2].headers).status_code for i in range(8)]
    acoes = [_agir((um, dois)[i % 2].headers, c.conta_id).status_code for i in range(8)]

    assert set(leituras) == {200}
    assert set(acoes) == {409}


# --- flag ligada -------------------------------------------------------------------------


def test_dois_operadores_no_mesmo_ip_dividem_o_limite_de_leitura(
    operador, flag_ligada, limite_por_ip_pequeno
):
    um, dois = operador(), operador(nome="Rui Operador")

    primeiras = [_ler(um.headers).status_code, _ler(dois.headers).status_code]
    terceira = _ler(um.headers).status_code
    quarta = _ler(dois.headers)  # o outro operador já não passa: o IP é o mesmo

    assert primeiras + [terceira] == [200, 200, 200]
    assert quarta.status_code == 429
    assert int(quarta.headers["Retry-After"]) >= 1


def test_ips_diferentes_nao_dividem_o_limite(operador, flag_ligada, limite_por_ip_pequeno):
    um, dois = operador(), operador(nome="Rui Operador")

    do_a = [_ler(um.headers, IP_A).status_code for _ in range(3)]
    do_b = [_ler(dois.headers, IP_B).status_code for _ in range(3)]

    assert do_a + do_b == [200] * 6
    assert _ler(um.headers, IP_A).status_code == 429
    assert _ler(dois.headers, IP_B).status_code == 429


def test_o_mesmo_operador_em_dois_ips_tem_um_contador_por_ip(
    operador, flag_ligada, limite_por_ip_pequeno
):
    um = operador()

    for _ in range(3):
        assert _ler(um.headers, IP_A).status_code == 200

    assert _ler(um.headers, IP_A).status_code == 429
    assert _ler(um.headers, IP_B).status_code == 200


def test_limite_de_acoes_por_ip_e_dividido_entre_operadores(
    operador, cliente, flag_ligada, limite_por_ip_pequeno
):
    um, dois = operador(), operador(nome="Rui Operador")
    c = cliente()

    ate_o_limite = [
        _agir(um.headers, c.conta_id).status_code,
        _agir(um.headers, c.conta_id).status_code,
        _agir(dois.headers, c.conta_id).status_code,
        _agir(dois.headers, c.conta_id).status_code,
    ]
    estourou = _agir(um.headers, c.conta_id)
    outro_ip = _agir(um.headers, c.conta_id, IP_B)

    # Cada operador está longe dos 10 por minuto dele; quem estoura é o IP.
    assert ate_o_limite == [409] * 4
    assert estourou.status_code == 429
    assert outro_ip.status_code == 409


def test_leitura_e_acao_tem_contadores_separados_por_ip(
    operador, cliente, flag_ligada, limite_por_ip_pequeno
):
    um = operador()
    c = cliente()

    for _ in range(3):
        assert _ler(um.headers).status_code == 200
    assert _ler(um.headers).status_code == 429

    assert _agir(um.headers, c.conta_id).status_code == 409


def test_o_limite_por_ip_e_de_todo_o_admin_nao_de_uma_rota_so(
    operador, cliente, flag_ligada, limite_por_ip_pequeno
):
    um = operador()
    c = cliente()
    por_uf = lambda: client.get(
        "/admin/contas/por-uf", headers=_com_ip(um.headers, IP_A)
    )
    lista = lambda: client.get("/admin/contas", headers=_com_ip(um.headers, IP_A))
    detalhe = lambda: client.get(
        f"/admin/contas/{c.conta_id}", headers=_com_ip(um.headers, IP_A)
    )

    gastos = [por_uf().status_code, lista().status_code, detalhe().status_code]

    assert gastos == [200, 200, 200]
    assert por_uf().status_code == 429
    assert lista().status_code == 429


def test_o_limite_por_usuario_continua_valendo_com_a_flag_ligada(operador, cliente, flag_ligada):
    """Os 30 por IP não escondem os 10 por usuário: o 11º do mesmo operador
    dá 429 com o IP longe do limite."""
    um = operador()
    c = cliente()

    codigos = [_agir(um.headers, c.conta_id).status_code for _ in range(11)]

    assert codigos == [409] * 10 + [429]


def test_flag_ligada_com_o_ip_repassado_pelo_front(
    operador, flag_ligada, limite_por_ip_pequeno, monkeypatch
):
    """Com o segredo do proxy, vale o `X-Ledgr-IP-Cliente`, não o IP da Vercel."""
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", "segredo-de-teste")
    um, dois = operador(), operador(nome="Rui Operador")
    vercel = {"X-Forwarded-For": "76.76.21.21", "X-Ledgr-Segredo-Proxy": "segredo-de-teste"}

    def ler(o, navegador):
        return client.get(
            "/admin/contas/por-uf", headers={**o.headers, **vercel, "X-Ledgr-IP-Cliente": navegador}
        ).status_code

    do_navegador_a = [ler(um, "198.51.100.1") for _ in range(3)]
    do_navegador_b = [ler(dois, "198.51.100.2") for _ in range(3)]

    assert do_navegador_a + do_navegador_b == [200] * 6
    assert ler(um, "198.51.100.1") == 429
    assert ler(dois, "198.51.100.2") == 429
