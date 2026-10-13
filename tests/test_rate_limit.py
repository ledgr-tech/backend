"""Chave do rate limit (issue #64).

Na Railway a conexão chega sempre pelo proxy dela, e quem chama o `/login`, o
`/register` e o `/senha/*` é o servidor do Next, na Vercel. A chave não pode
ser o IP da conexão (um contador só para o produto inteiro) nem um cabeçalho
que o cliente escreve sozinho (bastaria trocar o valor para nunca bater no
limite). Os testes de cada rota com limite ficam nos arquivos delas; aqui fica
a chave.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limit import (
    CABECALHO_IP_CLIENTE,
    CABECALHO_SEGREDO_PROXY,
    chave_por_empresa,
    chave_por_usuario,
    ip_do_cliente,
)
from main import app

client = TestClient(app)

SEGREDO = "segredo-do-proxy-de-teste-nao-usar-em-producao"


@pytest.fixture(autouse=True)
def _segredo_proxy(monkeypatch):
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", SEGREDO)


def _request(cabecalhos: dict[str, str], host: str = "10.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in cabecalhos.items()],
            "client": (host, 12345),
        }
    )


def _do_front(ip: str, segredo: str = SEGREDO) -> dict[str, str]:
    return {CABECALHO_IP_CLIENTE: ip, CABECALHO_SEGREDO_PROXY: segredo}


# --- ip_do_cliente --------------------------------------------------------------


def test_sem_cabecalho_usa_o_ip_da_conexao():
    assert ip_do_cliente(_request({}, host="10.0.0.1")) == "ip:10.0.0.1"


def test_usa_o_ultimo_item_do_x_forwarded_for():
    # o último é o que o proxy da Railway anotou; os anteriores vêm do cliente
    request = _request({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 203.0.113.9"})

    assert ip_do_cliente(request) == "ip:203.0.113.9"


def test_x_forwarded_for_forjado_nao_muda_a_chave():
    chaves = {
        ip_do_cliente(_request({"X-Forwarded-For": f"9.9.9.{n}, 203.0.113.9"})) for n in range(5)
    }

    assert chaves == {"ip:203.0.113.9"}


def test_x_forwarded_for_invalido_cai_no_ip_da_conexao():
    request = _request({"X-Forwarded-For": "1.1.1.1, nao-e-ip"}, host="10.0.0.1")

    assert ip_do_cliente(request) == "ip:10.0.0.1"


def test_ip_repassado_pelo_front_com_segredo_certo():
    cabecalhos = {"X-Forwarded-For": "76.76.21.21", **_do_front("198.51.100.7")}

    assert ip_do_cliente(_request(cabecalhos)) == "ip:198.51.100.7"


def test_ip_repassado_com_segredo_errado_e_ignorado():
    cabecalhos = {"X-Forwarded-For": "76.76.21.21", **_do_front("198.51.100.7", "outro")}

    assert ip_do_cliente(_request(cabecalhos)) == "ip:76.76.21.21"


def test_ip_repassado_sem_segredo_configurado_e_ignorado(monkeypatch):
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", "  ")
    # sem segredo no backend, um segredo vazio no cabeçalho não pode valer
    cabecalhos = {"X-Forwarded-For": "76.76.21.21", **_do_front("198.51.100.7", "")}

    assert ip_do_cliente(_request(cabecalhos)) == "ip:76.76.21.21"


def test_ip_repassado_invalido_e_ignorado():
    cabecalhos = {"X-Forwarded-For": "76.76.21.21", **_do_front("nao-e-ip")}

    assert ip_do_cliente(_request(cabecalhos)) == "ip:76.76.21.21"


def test_ip_repassado_aceita_ipv6():
    assert ip_do_cliente(_request(_do_front("2001:db8::1"))) == "ip:2001:db8::1"


# --- chaves por token -----------------------------------------------------------


def test_chave_por_empresa_usa_o_empresa_id_do_token(gerar_token):
    empresa_id = uuid.uuid4()
    request = _request({"Authorization": f"Bearer {gerar_token(empresa_id)}"})

    assert chave_por_empresa(request) == f"empresa:{empresa_id}"


def test_chave_por_empresa_com_token_forjado_cai_no_ip(gerar_token):
    token = gerar_token(uuid.uuid4(), secret="outro-secret-com-pelo-menos-32-bytes-de-tamanho")
    request = _request({"Authorization": f"Bearer {token}"}, host="10.0.0.1")

    assert chave_por_empresa(request) == "ip:10.0.0.1"


def test_chave_por_usuario_sem_token_usa_o_ip_do_cliente():
    assert chave_por_usuario(_request(_do_front("198.51.100.7"))) == "ip:198.51.100.7"


# --- nas rotas ------------------------------------------------------------------


def _login(cabecalhos: dict[str, str]) -> int:
    # Um e-mail por tentativa: com o mesmo, o bloqueio por conta (issue #67)
    # responderia 429 já na 6ª, e o que se testa aqui é o limite por IP.
    corpo = {"email": f"{uuid.uuid4().hex[:12]}@exemplo.com", "senha": "senha-errada-123"}
    return client.post("/login", json=corpo, headers=cabecalhos).status_code


def test_login_trocar_x_forwarded_for_nao_escapa_do_limite(db_session):
    codigos = [_login({"X-Forwarded-For": f"9.9.9.{n}, 203.0.113.9"}) for n in range(11)]

    assert codigos[:10] == [401] * 10
    assert codigos[10] == 429


def test_login_usuarios_diferentes_atras_do_front_tem_contadores_separados(db_session):
    # o servidor do Next chama sempre do mesmo IP da Vercel
    vercel = {"X-Forwarded-For": "76.76.21.21"}
    codigos_a = [_login({**vercel, **_do_front("198.51.100.7")}) for _ in range(11)]

    assert codigos_a[10] == 429
    assert _login({**vercel, **_do_front("198.51.100.8")}) == 401


def test_login_sem_segredo_todos_atras_do_front_dividem_o_limite(db_session):
    vercel = {"X-Forwarded-For": "76.76.21.21"}
    for _ in range(10):
        _login({**vercel, **_do_front("198.51.100.7", "errado")})

    assert _login({**vercel, **_do_front("198.51.100.8", "errado")}) == 429


def _upload(token: str, ip: str) -> int:
    return client.post(
        "/extratos/upload",
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": ip},
        files={"arquivo": ("extrato.pdf", b"x", "application/pdf")},
        data={"origem": "banco"},
    ).status_code


def test_upload_limita_por_empresa_e_nao_por_ip(gerar_token):
    empresa_a, empresa_b = gerar_token(uuid.uuid4()), gerar_token(uuid.uuid4())

    # extensão inválida: 400 sem tocar no banco, mas conta no limite
    codigos_a = [_upload(empresa_a, f"198.51.100.{n}") for n in range(11)]

    assert codigos_a[:10] == [400] * 10
    assert codigos_a[10] == 429
    # mesmo IP da última chamada da empresa A
    assert _upload(empresa_b, "198.51.100.10") == 400
