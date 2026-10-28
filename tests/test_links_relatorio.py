"""Link público do relatório do fechamento (issue #165, ADR-024)."""

import hashlib
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import SENHA_USUARIO_TESTE, cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import func, text, update

from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from app.core.database import engine
from app.core.rate_limit import CABECALHO_IP_CLIENTE, CABECALHO_SEGREDO_PROXY
from app.models import Empresa, LinkRelatorio
from app.services import relatorio_publico
from app.services.relatorio_publico import CAMPOS_DA_LINHA, CAMPOS_DO_RELATORIO
from app.services.travas import chave_do_mes
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
INVALIDO = {"detail": "Link inválido ou expirado."}
SEGREDO = "segredo-do-proxy-de-teste"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture(autouse=True)
def _flag_ligada(monkeypatch):
    monkeypatch.setattr(settings, "link_publico_habilitado", True)


@pytest.fixture
def conta(
    db_session,
    criar_usuario,
    criar_empresa,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
):
    """`conta()` monta admin, gestor e analista, e `fechar_setembro()` concilia
    um par, justifica uma linha e fecha o mês com ressalva."""

    def _nova(razao_social: str = "Escritório Contábil"):
        admin = criar_usuario(nome="Ana Admin")
        conta_id = admin.conta_id
        db_session.execute(
            update(Empresa).where(Empresa.conta_id == conta_id).values(razao_social=razao_social)
        )
        db_session.commit()
        usuarios = {
            "admin": admin,
            "gestor": criar_usuario(conta_id=conta_id, nome="Gil Gestor", papel="gestor"),
            "analista": criar_usuario(conta_id=conta_id, nome="Ada Analista", papel="analista"),
        }
        empresa_id = empresa_unica(db_session, conta_id)
        c = SimpleNamespace(usuarios=usuarios, conta_id=conta_id)

        def entrar(empresa):
            c.empresa_id = empresa
            c.headers = {
                papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id), empresa)
                for papel, u in usuarios.items()
            }

        entrar(empresa_id)

        def extrato(origem, itens, nome):
            novo = criar_extrato_da_empresa(c.empresa_id, origem)
            novo.nome_arquivo = nome
            inserir_lancamentos(novo, list(itens))
            novo.periodo_inicio, novo.periodo_fim = SETEMBRO
            db_session.commit()
            return novo

        def fechar_setembro():
            banco = extrato(
                "banco",
                [("10.00", "Pix recebido"), ("-20.00", "Tarifa do pacote"), ("-30.00", "Boleto")],
                "banco-setembro.ofx",
            )
            sistema = extrato("sistema", [("10.00", "Pix recebido")], "sistema-setembro.csv")
            resposta = client.post(
                "/conciliacoes",
                headers=c.headers["admin"],
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == 201, resposta.text
            itens = client.get(f"/conciliacoes/{banco.id}", headers=c.headers["admin"]).json()[
                "itens"
            ]
            tarifa = next(
                i["chave"]
                for i in itens
                if i["lancamento_banco"] and i["lancamento_banco"]["valor"] == "-20.00"
            )
            assert (
                client.post(
                    f"/conciliacoes/{banco.id}/decisoes",
                    headers=c.headers["gestor"],
                    json={"chave": tarifa, "tipo": "justificada", "texto": "Tarifa do mês"},
                ).status_code
                == 200
            )
            fechado = client.post(
                "/fechamentos",
                headers=c.headers["admin"],
                json={"competencia": "2026-09", "ressalva": "O boleto fica para outubro."},
            )
            assert fechado.status_code == 201, fechado.text
            c.banco, c.sistema = banco, sistema
            return fechado.json()

        c.entrar = entrar
        c.fechar_setembro = fechar_setembro
        return c

    return _nova


def _criar(headers, competencia="2026-09", **corpo):
    return client.post(f"/fechamentos/{competencia}/links", headers=headers, json=corpo or None)


def _listar(headers, competencia="2026-09"):
    return client.get(f"/fechamentos/{competencia}/links", headers=headers)


def _revogar(headers, link_id, competencia="2026-09"):
    return client.delete(f"/fechamentos/{competencia}/links/{link_id}", headers=headers)


def _consultar(token, headers=None):
    return client.post("/relatorios-publicos/consultar", json={"token": token}, headers=headers)


def _reabrir(headers, competencia="2026-09"):
    return client.delete(f"/fechamentos/{competencia}", headers=headers)


def _link(db_session, link_id) -> LinkRelatorio:
    db_session.expire_all()
    return db_session.get(LinkRelatorio, uuid.UUID(link_id))


# --- criar ----------------------------------------------------------------------------


@pytest.mark.parametrize("quem", ["admin", "gestor"])
def test_criar_devolve_o_token_uma_vez_e_guarda_so_o_hash(db_session, conta, quem):
    c = conta()
    c.fechar_setembro()

    resposta = _criar(c.headers[quem])

    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    token = corpo["token"]
    assert len(token) >= 43
    assert corpo["prefixo"] == token[:8]
    assert (corpo["criado_por"], corpo["acessos"], corpo["ativo"]) == (
        c.usuarios[quem].nome,
        0,
        True,
    )
    criado = datetime.fromisoformat(corpo["criado_em"])
    expira = datetime.fromisoformat(corpo["expira_em"])
    assert timedelta(days=30) - timedelta(minutes=1) < expira - criado <= timedelta(days=30)
    link = _link(db_session, corpo["id"])
    assert link.hash == hashlib.sha256(token.encode()).hexdigest()
    gravado = db_session.execute(
        text("SELECT row_to_json(l)::text FROM links_relatorio l WHERE id = :id"),
        {"id": link.id},
    ).scalar_one()
    assert token not in gravado
    assert "token" not in _listar(c.headers[quem]).json()["itens"][0]


@pytest.mark.parametrize(("dias", "esperado"), [(1, 201), (90, 201), (0, 422), (91, 422)])
def test_validade_entre_1_e_90_dias(db_session, conta, dias, esperado):
    c = conta()
    c.fechar_setembro()

    resposta = _criar(c.headers["admin"], validade_dias=dias)

    assert resposta.status_code == esperado
    if esperado == 201:
        corpo = resposta.json()
        prazo = datetime.fromisoformat(corpo["expira_em"]) - datetime.fromisoformat(
            corpo["criado_em"]
        )
        assert abs(prazo - timedelta(days=dias)) < timedelta(minutes=1)


def test_criar_sem_fechamento_ativo_e_404(db_session, conta):
    c = conta()

    resposta = _criar(c.headers["admin"])

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Não há fechamento ativo nesta competência."}
    assert _criar(c.headers["admin"], competencia="2026-13").status_code == 422


def test_analista_recebe_403_em_tudo(db_session, conta):
    c = conta()
    c.fechar_setembro()
    link_id = _criar(c.headers["admin"]).json()["id"]
    so_admin_ou_gestor = {"detail": "Só o admin ou o gestor da conta podem fazer isso."}

    for resposta in (
        _criar(c.headers["analista"]),
        _listar(c.headers["analista"]),
        _revogar(c.headers["analista"], link_id),
    ):
        assert (resposta.status_code, resposta.json()) == (403, so_admin_ou_gestor)
    assert _link(db_session, link_id).revogado_em is None


# --- limite de 5 ----------------------------------------------------------------------


def test_no_maximo_5_ativos_por_fechamento(db_session, conta):
    c = conta()
    c.fechar_setembro()
    ids = [_criar(c.headers["admin"]).json()["id"] for _ in range(5)]

    sexto = _criar(c.headers["gestor"])

    assert sexto.status_code == 409
    assert sexto.json() == {
        "detail": "Este fechamento já tem 5 links ativos. Revogue um antes de criar outro."
    }
    _revogar(c.headers["admin"], ids[0])
    db_session.execute(
        update(LinkRelatorio)
        .where(LinkRelatorio.id == uuid.UUID(ids[1]))
        .values(expira_em=func.now() - timedelta(seconds=1))
    )
    db_session.commit()
    # Revogado e vencido não contam.
    assert [_criar(c.headers["admin"]).status_code for _ in range(3)] == [201, 201, 409]


def _segurar(chave):
    conexao = engine.connect()
    transacao = conexao.begin()
    conexao.execute(text("SELECT pg_advisory_xact_lock(:chave)"), {"chave": chave})

    def soltar():
        transacao.commit()
        conexao.close()

    return soltar


def _ao_mesmo_tempo(chave, *funcoes):
    soltar = _segurar(chave)
    resultados: list = [None] * len(funcoes)
    threads = [
        threading.Thread(target=lambda i=i, f=f: resultados.__setitem__(i, f()))
        for i, f in enumerate(funcoes)
    ]
    try:
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        assert all(thread.is_alive() for thread in threads), "deveria esperar a trava do mês"
    finally:
        soltar()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return resultados


def test_duas_criacoes_ao_mesmo_tempo_nao_passam_do_limite(db_session, conta):
    c = conta()
    c.fechar_setembro()
    for _ in range(4):
        _criar(c.headers["admin"])

    respostas = _ao_mesmo_tempo(
        chave_do_mes(c.empresa_id, "2026-09"),
        lambda: _criar(c.headers["admin"]),
        lambda: _criar(c.headers["gestor"]),
    )

    assert sorted(r.status_code for r in respostas) == [201, 409]
    assert sum(item["ativo"] for item in _listar(c.headers["admin"]).json()["itens"]) == 5


def test_criar_ao_mesmo_tempo_que_reabrir_nao_deixa_link_ativo(db_session, conta):
    c = conta()
    c.fechar_setembro()
    for _ in range(4):
        _criar(c.headers["admin"])

    criar, reabrir = _ao_mesmo_tempo(
        chave_do_mes(c.empresa_id, "2026-09"),
        lambda: _criar(c.headers["gestor"]),
        lambda: _reabrir(c.headers["admin"]),
    )

    assert reabrir.status_code == 200
    assert criar.status_code in (201, 404)
    itens = _listar(c.headers["admin"]).json()["itens"]
    assert len(itens) == (5 if criar.status_code == 201 else 4)
    assert not any(item["ativo"] for item in itens)
    assert {item["motivo_revogacao"] for item in itens} == {"reabertura"}
    if criar.status_code == 201:
        assert _consultar(criar.json()["token"]).status_code == 404


# --- listar e revogar -----------------------------------------------------------------


def test_listar_dos_mais_novos_para_os_mais_antigos(db_session, conta):
    c = conta()
    c.fechar_setembro()
    primeiro = _criar(c.headers["admin"]).json()
    segundo = _criar(c.headers["gestor"], validade_dias=7).json()
    _consultar(segundo["token"])

    itens = _listar(c.headers["gestor"]).json()["itens"]

    assert [item["id"] for item in itens] == [segundo["id"], primeiro["id"]]
    assert set(itens[0]) == {
        "id",
        "fechamento_id",
        "prefixo",
        "criado_por",
        "criado_em",
        "expira_em",
        "ultimo_acesso_em",
        "acessos",
        "revogado_por",
        "revogado_em",
        "motivo_revogacao",
        "ativo",
    }
    assert (itens[0]["criado_por"], itens[0]["acessos"]) == ("Gil Gestor", 1)
    assert itens[0]["ultimo_acesso_em"] is not None
    assert (itens[1]["acessos"], itens[1]["ultimo_acesso_em"]) == (0, None)


def test_revogar_e_imediato_e_idempotente(db_session, conta):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["admin"]).json()
    assert _consultar(criado["token"]).status_code == 200

    primeira = _revogar(c.headers["gestor"], criado["id"])
    segunda = _revogar(c.headers["admin"], criado["id"])

    assert (primeira.status_code, segunda.status_code) == (204, 204)
    assert _consultar(criado["token"]).json() == INVALIDO
    (item,) = _listar(c.headers["admin"]).json()["itens"]
    assert (item["revogado_por"], item["motivo_revogacao"], item["ativo"]) == (
        "Gil Gestor",
        "manual",
        False,
    )
    assert _revogar(c.headers["admin"], uuid.uuid4()).json() == {"detail": "Link não encontrado."}
    assert _revogar(c.headers["admin"], criado["id"], competencia="2026-08").status_code == 404


def test_link_vencido_para_de_abrir(db_session, conta):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["admin"], validade_dias=1).json()
    db_session.execute(
        update(LinkRelatorio)
        .where(LinkRelatorio.id == uuid.UUID(criado["id"]))
        .values(expira_em=func.now() - timedelta(seconds=1))
    )
    db_session.commit()

    assert _consultar(criado["token"]).json() == INVALIDO
    assert _listar(c.headers["admin"]).json()["itens"][0]["ativo"] is False


def test_reabrir_revoga_com_o_motivo_e_fechar_de_novo_pede_link_novo(db_session, conta):
    c = conta()
    c.fechar_setembro()
    antigo = _criar(c.headers["admin"]).json()
    ja_revogado = _criar(c.headers["admin"]).json()
    _revogar(c.headers["admin"], ja_revogado["id"])

    assert _reabrir(c.headers["gestor"]).status_code == 200

    assert _consultar(antigo["token"]).json() == INVALIDO
    por_id = {item["id"]: item for item in _listar(c.headers["admin"]).json()["itens"]}
    assert (por_id[antigo["id"]]["motivo_revogacao"], por_id[antigo["id"]]["revogado_por"]) == (
        "reabertura",
        "Gil Gestor",
    )
    # O que já estava revogado não muda de motivo.
    assert por_id[ja_revogado["id"]]["motivo_revogacao"] == "manual"

    refechado = client.post(
        "/fechamentos",
        headers=c.headers["admin"],
        json={"competencia": "2026-09", "ressalva": "De novo."},
    )
    assert refechado.status_code == 201
    novo = _criar(c.headers["admin"]).json()
    assert _consultar(antigo["token"]).json() == INVALIDO
    assert _consultar(novo["token"]).json()["ressalva"] == "De novo."
    assert novo["fechamento_id"] != antigo["fechamento_id"]
    assert len(_listar(c.headers["admin"]).json()["itens"]) == 3


# --- a consulta pública ---------------------------------------------------------------


def test_relatorio_tem_exatamente_os_campos_da_lista(db_session, conta):
    c = conta()
    c.fechar_setembro()
    token = _criar(c.headers["admin"]).json()["token"]

    corpo = _consultar(token).json()

    assert set(corpo) == {
        "empresa",
        "competencia",
        "situacao",
        "fechado_em",
        "fechado_por",
        "ressalva",
        "resumo",
        "pares",
        "link",
    }
    assert set(corpo["empresa"]) == set(CAMPOS_DO_RELATORIO["empresa"])
    assert set(corpo["resumo"]) == set(CAMPOS_DO_RELATORIO["resumo"])
    assert set(corpo["resumo"]["categorias"]) == {
        "duplicado",
        "sem_correspondencia",
        "tarifa_bancaria",
        "divergente_valor",
        "divergente_data",
    }
    assert set(corpo["link"]) == set(CAMPOS_DO_RELATORIO["link"])
    (par,) = corpo["pares"]
    assert set(par) == set(CAMPOS_DO_RELATORIO["par"])
    for linha in par["justificadas"] + par["pendentes"]:
        assert set(linha) == set(CAMPOS_DA_LINHA)
    # Nenhum identificador interno nem e-mail em lugar nenhum.
    bruto = _consultar(token).text
    assert not UUID.search(bruto)
    assert "@" not in bruto


def test_relatorio_traz_o_que_o_pdf_mostra(db_session, conta):
    c = conta(razao_social="Padaria do Cliente Ltda")
    fechado = c.fechar_setembro()
    token = _criar(c.headers["admin"]).json()["token"]

    corpo = _consultar(token).json()

    empresa = db_session.get(Empresa, c.empresa_id)
    assert corpo["empresa"] == {"razao_social": "Padaria do Cliente Ltda", "cnpj": empresa.cnpj}
    assert (corpo["competencia"], corpo["situacao"]) == ("2026-09", "fechado_com_ressalva")
    assert (corpo["fechado_por"], corpo["ressalva"]) == ("Ana Admin", "O boleto fica para outubro.")
    assert datetime.fromisoformat(corpo["fechado_em"]) == datetime.fromisoformat(
        fechado["fechado_em"]
    )
    assert corpo["resumo"] == {
        "total": 3,
        "conciliadas": 1,
        "justificadas": 1,
        "pendentes": 1,
        "linhas_nao_lidas": 0,
        "valor_em_aberto": "30.00",
        "categorias": {
            "duplicado": 0,
            "sem_correspondencia": 1,
            "tarifa_bancaria": 1,
            "divergente_valor": 0,
            "divergente_data": 0,
        },
    }
    (par,) = corpo["pares"]
    assert (par["arquivo_banco"], par["arquivo_sistema"], par["rodada"], par["truncada"]) == (
        "banco-setembro.ofx",
        "sistema-setembro.csv",
        1,
        False,
    )
    (justificada,) = par["justificadas"]
    assert {k: v for k, v in justificada.items() if k != "justificada_em"} == {
        "data": "2026-09-05",
        "descricao": "Tarifa do pacote",
        "valor_banco": "-20.00",
        "valor_sistema": None,
        "situacao": "tarifa_bancaria",
        "justificativa": "Tarifa do mês",
        "justificada_por": "Gil Gestor",
    }
    assert justificada["justificada_em"] is not None
    (pendente,) = par["pendentes"]
    assert (pendente["descricao"], pendente["valor_banco"], pendente["justificativa"]) == (
        "Boleto",
        "-30.00",
        None,
    )
    assert corpo["link"]["expira_em"]


def test_relatorio_trunca_no_teto_por_par(db_session, conta, monkeypatch):
    c = conta()
    c.fechar_setembro()
    token = _criar(c.headers["admin"]).json()["token"]
    monkeypatch.setattr(relatorio_publico, "TETO_DE_LINHAS_POR_PAR", 1)

    (par,) = _consultar(token).json()["pares"]

    assert len(par["justificadas"]) + len(par["pendentes"]) == 1
    assert par["truncada"] is True


def test_acesso_soma_e_grava_a_hora_e_os_cabecalhos(db_session, conta):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["admin"]).json()

    respostas = [_consultar(criado["token"]) for _ in range(3)]
    invalida = _consultar("x" * 43)

    link = _link(db_session, criado["id"])
    assert link.acessos == 3
    assert link.ultimo_acesso_em is not None
    for resposta in [*respostas, invalida]:
        assert resposta.headers["cache-control"] == "no-store"
        assert resposta.headers["x-robots-tag"] == "noindex, nofollow"
        assert resposta.headers["referrer-policy"] == "no-referrer"


def test_404_identico_em_todos_os_casos_invalidos(db_session, conta, monkeypatch):
    c = conta()
    c.fechar_setembro()
    revogado = _criar(c.headers["admin"]).json()
    _revogar(c.headers["admin"], revogado["id"])
    vencido = _criar(c.headers["admin"]).json()
    db_session.execute(
        update(LinkRelatorio)
        .where(LinkRelatorio.id == uuid.UUID(vencido["id"]))
        .values(expira_em=func.now() - timedelta(seconds=1))
    )
    db_session.commit()
    valido = _criar(c.headers["admin"]).json()
    reaberto_conta = conta()
    reaberto_conta.fechar_setembro()
    reaberto = _criar(reaberto_conta.headers["admin"]).json()
    _reabrir(reaberto_conta.headers["admin"])

    casos = {
        "inexistente": _consultar(relatorio_publico.gerar_token().token),
        "vazio": _consultar(""),
        "prefixo_certo": _consultar(valido["token"][:8] + "A" * 35),
        "revogado": _consultar(revogado["token"]),
        "vencido": _consultar(vencido["token"]),
        "reaberto": _consultar(reaberto["token"]),
    }
    monkeypatch.setattr(settings, "link_publico_habilitado", False)
    casos["flag_desligada"] = _consultar(valido["token"])

    for nome, resposta in casos.items():
        assert resposta.status_code == 404, nome
        assert resposta.content == casos["inexistente"].content, nome
        assert dict(resposta.headers) == dict(casos["inexistente"].headers), nome
    assert _link(db_session, valido["id"]).acessos == 0


def test_limite_por_ip(db_session, conta, monkeypatch):
    monkeypatch.setattr(settings, "ledgr_segredo_proxy", SEGREDO)
    c = conta()
    c.fechar_setembro()
    token = _criar(c.headers["admin"]).json()["token"]

    def de(ip):
        return {CABECALHO_SEGREDO_PROXY: SEGREDO, CABECALHO_IP_CLIENTE: ip}

    respostas = [_consultar(token, de("203.0.113.7")).status_code for _ in range(31)]
    outro_ip = _consultar(token, de("198.51.100.1"))

    assert respostas == [200] * 30 + [429]
    assert outro_ip.status_code == 200


# --- flag desligada -------------------------------------------------------------------


def test_flag_desligada_criar_e_consultar_404_listar_e_revogar_seguem(
    db_session, conta, monkeypatch
):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["admin"]).json()
    monkeypatch.setattr(settings, "link_publico_habilitado", False)

    criar = _criar(c.headers["admin"])
    consultar = _consultar(criado["token"])
    listar = _listar(c.headers["admin"])
    revogar = _revogar(c.headers["admin"], criado["id"])

    assert (criar.status_code, criar.json()) == (404, {"detail": "Link público não disponível."})
    assert (consultar.status_code, consultar.json()) == (404, INVALIDO)
    assert listar.status_code == 200 and len(listar.json()["itens"]) == 1
    assert revogar.status_code == 204
    assert _link(db_session, criado["id"]).motivo_revogacao == "manual"


# --- isolamento -----------------------------------------------------------------------


def test_isolamento_entre_empresas_e_contas(db_session, conta, criar_empresa):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["admin"]).json()
    primeira = c.empresa_id
    outra_conta = conta()
    outra_conta.fechar_setembro()
    segunda = criar_empresa("Cliente 2", conta_id=c.conta_id)
    c.entrar(segunda.id)

    na_outra_empresa = [
        _listar(c.headers["admin"]),
        _revogar(c.headers["admin"], criado["id"]),
        _criar(c.headers["admin"]),
    ]
    de_outra_conta = [
        _listar(outra_conta.headers["admin"]),
        _revogar(outra_conta.headers["admin"], criado["id"]),
    ]
    com_a_empresa_de_outra_conta = _listar(
        {**outra_conta.headers["admin"], CABECALHO_EMPRESA: str(primeira)}
    )

    assert na_outra_empresa[0].json() == {"itens": []}
    assert na_outra_empresa[1].json() == {"detail": "Link não encontrado."}
    assert na_outra_empresa[2].json() == {"detail": "Não há fechamento ativo nesta competência."}
    assert [item["id"] for item in de_outra_conta[0].json()["itens"]] != [criado["id"]]
    assert de_outra_conta[1].status_code == 404
    assert com_a_empresa_de_outra_conta.json() == {"detail": "Empresa não encontrada."}
    assert _link(db_session, criado["id"]).revogado_em is None
    # O relatório é só da empresa do link.
    assert _consultar(criado["token"]).json()["empresa"]["razao_social"] == "Escritório Contábil"


# --- quem criou sai da conta ----------------------------------------------------------


def test_link_continua_quando_quem_criou_desativa_a_conta(db_session, conta):
    c = conta()
    c.fechar_setembro()
    criado = _criar(c.headers["gestor"]).json()

    saiu = client.request(
        "DELETE", "/me", json={"senha_atual": SENHA_USUARIO_TESTE}, headers=c.headers["gestor"]
    )

    assert saiu.status_code == 204, saiu.text
    assert _consultar(criado["token"]).status_code == 200
    (item,) = _listar(c.headers["admin"]).json()["itens"]
    assert (item["criado_por"], item["ativo"]) == ("Gil Gestor", True)


def test_log_nao_tem_token_nem_descricao(db_session, conta, caplog):
    c = conta()
    c.fechar_setembro()

    with caplog.at_level(logging.DEBUG):
        criado = _criar(c.headers["admin"]).json()
        _consultar(criado["token"])
        _consultar("token-que-nao-existe")
        _revogar(c.headers["admin"], criado["id"])

    assert criado["token"] not in caplog.text
    assert "Tarifa do pacote" not in caplog.text
    assert criado["prefixo"] in caplog.text
