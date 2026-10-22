"""Servidor MCP remoto (issue #136, parte 2; ADR-022).

O cliente é o do próprio SDK (`mcp.Client` sobre `streamable_http_client`),
falando com o app em processo por um `httpx2.ASGITransport`, com o token
emitido pelo fluxo OAuth da parte 1. O lifespan do app sobe junto, como no
uvicorn. Nenhum teste acessa a internet.
"""

import json
import logging
from datetime import date

import anyio
import httpx2
import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from test_mcp_oauth import (
    CALLBACK_CLAUDE,
    ISSUER,
    _pkce,
    _query,
    autorizar,
    mcp,  # noqa: F401 - fixture usada com usefixtures
    registrar,
    trocar_codigo,
)

from app.core.config import settings
from app.models import ConexaoMcp, Usuario
from main import app

BASE = "https://api.ledgr.teste"
DESCRICAO_SECRETA = "PAGAMENTO FORNECEDOR SIGILOSO"
JUSTIFICATIVA_SECRETA = "Texto confidencial da justificativa"
RESSALVA_SECRETA = "Ressalva confidencial do fechamento"
NOME_DA_PESSOA = "Joana Pessoa Secreta"
VALOR_SO_DA_B = "777.77"
FERRAMENTAS = {
    "empresa_conectada",
    "meses_com_conciliacao",
    "resumo_do_mes",
    "conciliacoes_do_mes",
    "divergencias_do_mes",
    "fechamentos",
}
WWW_AUTHENTICATE = (
    f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource", '
    'scope="ledgr.leitura"'
)


def _com_cliente(token: str, funcao, mode: str = "auto"):
    """Roda `funcao(cliente)` com um cliente MCP do SDK conectado ao /mcp."""

    async def _rodar():
        async with app.router.lifespan_context(app):
            transporte = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transporte,
                base_url=BASE,
                headers={"Authorization": f"Bearer {token}"},
            ) as http:
                transporte_mcp = streamable_http_client(f"{BASE}/mcp", http_client=http)
                async with Client(transporte_mcp, mode=mode) as cliente:
                    return await funcao(cliente)

    return anyio.run(_rodar)


def _chamar(token: str, nome: str, argumentos: dict | None = None):
    async def _funcao(cliente):
        return await cliente.call_tool(nome, argumentos or {})

    return _com_cliente(token, _funcao)


def _todas(token: str, competencia: str = "2026-09") -> dict:
    """Chama as 6 ferramentas numa conexão só e devolve os resultados."""

    async def _funcao(cliente):
        chamadas = {
            "empresa_conectada": {},
            "meses_com_conciliacao": {},
            "resumo_do_mes": {"competencia": competencia},
            "conciliacoes_do_mes": {"competencia": competencia},
            "divergencias_do_mes": {"competencia": competencia, "limite": 200},
            "fechamentos": {},
        }
        return {nome: await cliente.call_tool(nome, args) for nome, args in chamadas.items()}

    return _com_cliente(token, _funcao)


def _texto(resultado) -> str:
    return " ".join(bloco.text for bloco in resultado.content if bloco.type == "text")


@pytest.fixture
def cenario(
    db_session,
    criar_usuario,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
):
    """Conta com duas empresas, A e B, cada uma com um mês conciliado,
    justificado, conferido e fechado com ressalva; e fábricas de conexão."""
    from main import app as app_http

    cliente_http = TestClient(app_http)
    admin = criar_usuario(nome=NOME_DA_PESSOA)
    empresa_a = empresa_unica(db_session, admin.conta_id)
    empresa_b = criar_empresa(razao_social="Empresa B Ltda", conta_id=admin.conta_id).id
    jwt = gerar_token(admin.conta_id, usuario_id=admin.id)

    def semear(empresa_id, valores, so_no_sistema):
        headers = cabecalhos(jwt, empresa_id)
        extratos = {}
        for origem, itens in (("banco", valores), ("sistema", [valores[0], so_no_sistema])):
            novo = criar_extrato_da_empresa(empresa_id, origem)
            inserir_lancamentos(novo, itens)
            novo.periodo_inicio, novo.periodo_fim = date(2026, 9, 1), date(2026, 9, 30)
            db_session.commit()
            extratos[origem] = novo
        resposta = cliente_http.post(
            "/conciliacoes",
            headers=headers,
            json={
                "extrato_banco_id": str(extratos["banco"].id),
                "extrato_sistema_id": str(extratos["sistema"].id),
            },
        )
        assert resposta.status_code == 201, resposta.text
        itens = cliente_http.get(f"/conciliacoes/{extratos['banco'].id}", headers=headers).json()[
            "itens"
        ]

        def decidir(valor, tipo, texto=None):
            chave = next(
                i["chave"]
                for i in itens
                if i["lancamento_banco"] and i["lancamento_banco"]["valor"] == valor
            )
            r = cliente_http.post(
                f"/conciliacoes/{extratos['banco'].id}/decisoes",
                headers=headers,
                json={"chave": chave, "tipo": tipo, "texto": texto},
            )
            assert r.status_code == 200, r.text

        decidir(valores[1][0], "justificada", JUSTIFICATIVA_SECRETA)
        decidir(valores[2][0], "conferida")
        fechado = cliente_http.post(
            "/fechamentos",
            headers=headers,
            json={"competencia": "2026-09", "ressalva": RESSALVA_SECRETA},
        )
        assert fechado.status_code == 201, fechado.text
        return extratos["banco"].id

    banco_a = semear(
        empresa_a,
        [("10.00", "a"), ("20.00", DESCRICAO_SECRETA), ("-5.50", "c"), ("33.30", "d")],
        ("7.00", DESCRICAO_SECRETA),
    )
    banco_b = semear(
        empresa_b,
        [("10.00", "a"), (VALOR_SO_DA_B, DESCRICAO_SECRETA), ("-6.60", "c"), ("44.40", "d")],
        ("8.00", DESCRICAO_SECRETA),
    )

    def conectar(empresa_id=empresa_a, usuario: Usuario = admin):
        client_id = registrar().json()["client_id"]
        verifier, challenge = _pkce()
        destino = autorizar(client_id, CALLBACK_CLAUDE, challenge).headers["location"]
        aprovada = cliente_http.post(
            f"/conexoes-mcp/pedidos/{_query(destino)['pedido']}/aprovar",
            headers=cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id)),
            json={"empresa_id": str(empresa_id)},
        )
        assert aprovada.status_code == 200, aprovada.text
        code = _query(aprovada.json()["redirecionar_para"])["code"]
        return trocar_codigo(client_id, code, verifier).json()["access_token"]

    def chave_api():
        resposta = cliente_http.post(
            "/chaves-api", headers=cabecalhos(jwt), json={"nome": "Comparação"}
        )
        return resposta.json()["chave"]

    class _Cenario:
        pass

    c = _Cenario()
    c.admin, c.jwt, c.empresa_a, c.empresa_b = admin, jwt, empresa_a, empresa_b
    c.banco_a, c.banco_b = banco_a, banco_b
    c.conectar, c.chave_api, c.http = conectar, chave_api, cliente_http
    return c


# --- protocolo -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.usefixtures("mcp")
def test_initialize_e_lista_as_6_ferramentas_so_de_leitura(cenario, mode):
    token = cenario.conectar()

    async def _funcao(cliente):
        return cliente.server_info, cliente.protocol_version, (await cliente.list_tools()).tools

    info, versao, ferramentas = _com_cliente(token, _funcao, mode=mode)

    assert info is None or info.name == "ledgr"
    assert versao == ("2025-11-25" if mode == "legacy" else "2026-07-28")
    assert {f.name for f in ferramentas} == FERRAMENTAS
    for ferramenta in ferramentas:
        assert ferramenta.annotations.read_only_hint is True
        assert ferramenta.annotations.destructive_hint is False
        assert ferramenta.annotations.open_world_hint is False
        assert ferramenta.output_schema is not None
        assert ferramenta.description
        assert "empresa_id" not in (ferramenta.input_schema.get("properties") or {})


@pytest.mark.usefixtures("mcp")
def test_cada_ferramenta_responde(cenario):
    resultados = _todas(cenario.conectar())

    for nome, resultado in resultados.items():
        assert resultado.is_error is not True, (nome, _texto(resultado))
        assert resultado.structured_content is not None, nome
        assert _texto(resultado), nome
    empresa = resultados["empresa_conectada"].structured_content
    assert set(empresa) == {"razao_social", "cnpj"}
    meses = resultados["meses_com_conciliacao"].structured_content
    assert meses == {"itens": [{"competencia": "2026-09", "fechado": True}]}
    resumo = resultados["resumo_do_mes"]
    assert resumo.structured_content["fechado"] is True
    assert _texto(resumo) == (
        "Setembro de 2026: 4 divergências, 3 sem justificativa, mês fechado com ressalva."
    )
    assert resultados["divergencias_do_mes"].structured_content["total"] == 4


@pytest.mark.usefixtures("mcp")
def test_saidas_batem_com_a_v1(cenario):
    resultados = _todas(cenario.conectar())
    chave = {"Authorization": f"Bearer {cenario.chave_api()}"}
    base = f"/v1/empresas/{cenario.empresa_a}"

    def v1(caminho, **params):
        resposta = cenario.http.get(f"{base}{caminho}", headers=chave, params=params)
        assert resposta.status_code == 200, resposta.text
        return resposta.json()

    assert resultados["resumo_do_mes"].structured_content == v1("/resumo", competencia="2026-09")
    assert resultados["conciliacoes_do_mes"].structured_content == v1(
        "/conciliacoes", competencia="2026-09"
    )
    assert resultados["divergencias_do_mes"].structured_content == v1(
        "/divergencias", competencia="2026-09", limit=200
    )
    assert resultados["fechamentos"].structured_content == v1("/fechamentos")


@pytest.mark.usefixtures("mcp")
def test_conexao_da_empresa_a_nunca_devolve_dado_da_b(cenario):
    resultados = _todas(cenario.conectar(empresa_id=cenario.empresa_a))

    tudo = json.dumps(
        {n: [r.structured_content, _texto(r)] for n, r in resultados.items()}, ensure_ascii=False
    )
    assert VALOR_SO_DA_B not in tudo and "Empresa B" not in tudo
    assert str(cenario.banco_b) not in tudo
    assert str(cenario.banco_a) in tudo
    da_b = _todas(cenario.conectar(empresa_id=cenario.empresa_b))
    assert da_b["empresa_conectada"].structured_content["razao_social"] == "Empresa B Ltda"
    assert VALOR_SO_DA_B in json.dumps(da_b["divergencias_do_mes"].structured_content)


@pytest.mark.usefixtures("mcp")
def test_saidas_nao_tem_descricao_justificativa_ressalva_nomes_nem_arquivo(cenario):
    resultados = _todas(cenario.conectar())

    for nome, resultado in resultados.items():
        saida = json.dumps([resultado.structured_content, _texto(resultado)], ensure_ascii=False)
        for proibido in (
            DESCRICAO_SECRETA,
            JUSTIFICATIVA_SECRETA,
            RESSALVA_SECRETA,
            NOME_DA_PESSOA,
            "extrato.csv",
        ):
            assert proibido not in saida, (nome, proibido)


@pytest.mark.parametrize(
    ("nome", "argumentos", "trecho"),
    [
        ("resumo_do_mes", {"competencia": "2026-13"}, "Competência inválida"),
        ("resumo_do_mes", {"competencia": "setembro"}, "Competência inválida"),
        ("conciliacoes_do_mes", {"competencia": "2026-9"}, "Competência inválida"),
        ("fechamentos", {"competencia": "26-09"}, "Competência inválida"),
        (
            "divergencias_do_mes",
            {"competencia": "2026-09", "categoria": "x"},
            "Categoria desconhecida",
        ),
        (
            "divergencias_do_mes",
            {"competencia": "2026-09", "situacao": "x"},
            "Situação desconhecida",
        ),
        ("divergencias_do_mes", {"competencia": "2026-09", "limite": 201}, "limite vai de 1 a 200"),
        ("divergencias_do_mes", {"competencia": "2026-09", "limite": 0}, "limite vai de 1 a 200"),
        ("divergencias_do_mes", {"competencia": "2026-09", "deslocamento": -1}, "deslocamento"),
    ],
)
@pytest.mark.usefixtures("mcp")
def test_entrada_invalida_e_erro_da_ferramenta(cenario, nome, argumentos, trecho):
    resultado = _chamar(cenario.conectar(), nome, argumentos)

    assert resultado.is_error is True
    assert trecho in _texto(resultado)


@pytest.mark.usefixtures("mcp")
def test_divergencias_com_filtros_e_paginacao(cenario):
    token = cenario.conectar()

    async def _funcao(cliente):
        pagina = await cliente.call_tool(
            "divergencias_do_mes", {"competencia": "2026-09", "limite": 2, "deslocamento": 1}
        )
        pendentes = await cliente.call_tool(
            "divergencias_do_mes", {"competencia": "2026-09", "situacao": "sem_justificativa"}
        )
        return pagina, pendentes

    pagina, pendentes = _com_cliente(token, _funcao)

    assert pagina.structured_content["total"] == 4
    assert len(pagina.structured_content["itens"]) == 2
    assert "Há mais páginas" in _texto(pagina)
    assert pendentes.structured_content["total"] == 2
    assert {i["situacao"] for i in pendentes.structured_content["itens"]} == {"sem_justificativa"}


@pytest.mark.usefixtures("mcp")
def test_mes_sem_conciliacao(cenario):
    resultado = _chamar(cenario.conectar(), "resumo_do_mes", {"competencia": "2026-10"})

    assert resultado.is_error is not True
    assert _texto(resultado) == "Outubro de 2026: sem conciliação, mês aberto."


# --- porta de entrada ----------------------------------------------------------------------


def _post_mcp(cliente: TestClient, token: str | None = None, **cabecalhos_extra):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(cabecalhos_extra)
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    return cliente.post("/mcp", headers=headers, content=json.dumps(corpo))


@pytest.mark.usefixtures("mcp")
def test_sem_token_da_401_com_www_authenticate():
    with TestClient(app, base_url=BASE) as cliente:
        resposta = _post_mcp(cliente)
        get = cliente.get("/mcp")

    assert resposta.status_code == 401
    assert resposta.headers["www-authenticate"] == WWW_AUTHENTICATE
    assert get.status_code == 401


@pytest.mark.usefixtures("mcp")
def test_caminho_exato_sem_redirecionar(cenario):
    token = cenario.conectar()
    with TestClient(app, base_url=BASE, follow_redirects=False) as cliente:
        resposta = _post_mcp(cliente, token)

    assert resposta.status_code not in (301, 302, 307, 308, 401, 404)


@pytest.mark.usefixtures("mcp")
def test_token_revogado_da_401_na_chamada_seguinte(cenario, db_session):
    token = cenario.conectar()
    assert _chamar(token, "empresa_conectada").is_error is not True
    conexao_id = db_session.scalar(
        select(ConexaoMcp.id).where(ConexaoMcp.empresa_id == cenario.empresa_a)
    )

    revogada = cenario.http.delete(f"/conexoes-mcp/{conexao_id}", headers=cabecalhos(cenario.jwt))

    assert revogada.status_code == 204
    with TestClient(app, base_url=BASE) as cliente:
        resposta = _post_mcp(cliente, token)
    assert resposta.status_code == 401
    assert resposta.headers["www-authenticate"] == WWW_AUTHENTICATE


@pytest.mark.usefixtures("mcp")
def test_jwt_chave_de_api_e_token_de_outro_resource_dao_401(cenario, monkeypatch):
    token = cenario.conectar()
    chave = cenario.chave_api()

    with TestClient(app, base_url=BASE) as cliente:
        for credencial in (cenario.jwt, chave, "ledgr_mcp_inexistente"):
            assert _post_mcp(cliente, credencial).status_code == 401
        assert _post_mcp(cliente, token).status_code != 401
        monkeypatch.setattr(settings, "mcp_url_publica", "https://outro.ledgr.teste/mcp")
        resposta = _post_mcp(cliente, token)

    assert resposta.status_code == 401


def test_flag_desligada_da_404():
    with TestClient(app, base_url=BASE) as cliente:
        assert _post_mcp(cliente, "ledgr_mcp_qualquer").status_code == 404
        assert cliente.get("/mcp").status_code == 404


def test_sem_urls_configuradas_da_503(monkeypatch):
    monkeypatch.setattr(settings, "mcp_habilitado", True)
    with TestClient(app, base_url=BASE) as cliente:
        assert _post_mcp(cliente, "ledgr_mcp_qualquer").status_code == 503


@pytest.mark.usefixtures("mcp")
def test_origin_de_fora_da_403(cenario):
    token = cenario.conectar()
    with TestClient(app, base_url=BASE) as cliente:
        estranha = _post_mcp(cliente, token, Origin="https://evil.example")
        claude = _post_mcp(cliente, token, Origin="https://claude.ai")
        propria = _post_mcp(cliente, token, Origin=BASE)

    assert estranha.status_code == 403
    assert claude.status_code not in (401, 403)
    assert propria.status_code not in (401, 403)


@pytest.mark.usefixtures("mcp")
def test_rate_limit_por_conexao(cenario):
    token, outro = cenario.conectar(), cenario.conectar()
    with TestClient(app, base_url=BASE) as cliente:
        status = [_post_mcp(cliente, token).status_code for _ in range(60)]
        estourou = _post_mcp(cliente, token)
        da_outra = _post_mcp(cliente, outro)

    assert 429 not in status
    assert estourou.status_code == 429
    assert 1 <= int(estourou.headers["retry-after"]) <= 60
    assert da_outra.status_code != 429


@pytest.mark.usefixtures("mcp")
def test_ultimo_uso_da_conexao_e_gravado(cenario, db_session):
    token = cenario.conectar()

    _chamar(token, "empresa_conectada")

    db_session.expire_all()
    conexao = db_session.scalar(
        select(ConexaoMcp).where(ConexaoMcp.empresa_id == cenario.empresa_a)
    )
    assert conexao.ultimo_uso_em is not None


@pytest.mark.usefixtures("mcp")
def test_logs_com_ferramenta_e_conexao_sem_token_nem_conteudo(cenario, caplog, db_session):
    caplog.set_level(logging.INFO)
    token = cenario.conectar()
    conexao_id = db_session.scalar(
        select(ConexaoMcp.id).where(ConexaoMcp.empresa_id == cenario.empresa_a)
    )

    _todas(token)

    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert f"mcp_ferramenta nome=resumo_do_mes conexao_id={conexao_id} resultado=ok" in texto
    assert token not in texto and token[len("ledgr_mcp_") :] not in texto
    for conteudo in ("Setembro de 2026", "33.30", str(cenario.banco_a)):
        assert conteudo not in texto


@pytest.mark.usefixtures("mcp")
def test_criador_removido_corta_o_mcp(cenario, criar_usuario):
    gestor = criar_usuario(conta_id=cenario.admin.conta_id, papel="gestor", nome="Outra Pessoa")
    token = cenario.conectar(usuario=gestor)
    assert _chamar(token, "empresa_conectada").is_error is not True

    removido = cenario.http.delete(
        f"/empresa/usuarios/{gestor.id}", headers=cabecalhos(cenario.jwt)
    )

    assert removido.status_code == 204
    with TestClient(app, base_url=BASE) as cliente:
        assert _post_mcp(cliente, token).status_code == 401
