"""Aprovação de justificativas e fechamento por papel (issue #132, ADR-019).

Toda justificativa nasce aguardando aprovação; só admin e gestor aprovam,
rejeitam, fecham e reabrem; o mês não fecha com justificativa aguardando
aprovação, nem com ressalva. Cenário: uma conta com admin, gestor e analista,
uma empresa e pares conciliados pela API em setembro de 2026.
"""

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.justificativas import MAXIMO_ITENS
from app.core.auth import CABECALHO_EMPRESA
from app.core.config import settings
from app.models import DecisaoLinha, Fechamento
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
OUTUBRO = (date(2026, 10, 1), date(2026, 10, 31))
SO_ADMIN_OU_GESTOR = {"detail": "Só o admin ou o gestor da conta podem fazer isso."}
SEM_AGUARDANDO = {"detail": "Não há justificativa aguardando aprovação nesta linha."}


def _aguardando(n: int) -> dict:
    texto = (
        "justificativa aguardando aprovação" if n == 1 else "justificativas aguardando aprovação"
    )
    return {"detail": f"Há {n} {texto} nesta competência. Aprove ou rejeite antes de fechar."}


@pytest.fixture(autouse=True)
def _aprovacao_ligada(monkeypatch):
    """Os testes deste arquivo são da flag ligada, salvo os do fim."""
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)


@pytest.fixture
def cenario(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """`cenario()` monta a conta; `par(...)` concilia um par pela API."""

    def _novo(papeis=("admin", "gestor", "analista")):
        admin = criar_usuario(nome="Ana Admin")
        usuarios = {"admin": admin}
        for papel in papeis:
            if papel != "admin":
                usuarios[papel] = criar_usuario(
                    conta_id=admin.conta_id, nome=f"{papel.title()} da Silva", papel=papel
                )
        empresa_id = empresa_unica(db_session, admin.conta_id)
        headers = {
            papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id), empresa_id)
            for papel, u in usuarios.items()
        }

        def extrato(origem, itens, periodo):
            novo = criar_extrato_da_empresa(empresa_id, origem)
            inserir_lancamentos(novo, list(itens))
            novo.periodo_inicio, novo.periodo_fim = periodo
            db_session.commit()
            return novo

        def par(itens_banco, itens_sistema=(("10.00", "a"),), periodo=SETEMBRO):
            banco = extrato("banco", itens_banco, periodo)
            sistema = extrato("sistema", itens_sistema, periodo)
            resposta = client.post(
                "/conciliacoes",
                headers=headers["admin"],
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == 201, resposta.text
            return banco

        def listar(banco, quem="admin"):
            resposta = client.get(f"/conciliacoes/{banco.id}", headers=headers[quem])
            assert resposta.status_code == 200
            return resposta.json()

        def chave(banco, valor):
            return next(
                item["chave"]
                for item in listar(banco)["itens"]
                if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == valor
            )

        def item(banco, valor):
            return next(
                i
                for i in listar(banco)["itens"]
                if i["lancamento_banco"] and i["lancamento_banco"]["valor"] == valor
            )

        def decidir(quem, banco, valor, tipo, texto=None):
            return client.post(
                f"/conciliacoes/{banco.id}/decisoes",
                headers=headers[quem],
                json={"chave": chave(banco, valor), "tipo": tipo, "texto": texto},
            )

        def fechar(quem="admin", competencia="2026-09", ressalva=None):
            corpo = {"competencia": competencia}
            if ressalva is not None:
                corpo["ressalva"] = ressalva
            return client.post("/fechamentos", headers=headers[quem], json=corpo)

        def reabrir(quem="admin", competencia="2026-09"):
            return client.delete(f"/fechamentos/{competencia}", headers=headers[quem])

        def lote(quem="admin", **corpo):
            return client.post("/justificativas/lote", headers=headers[quem], json=corpo)

        return SimpleNamespace(
            usuarios=usuarios,
            headers=headers,
            empresa_id=empresa_id,
            par=par,
            listar=listar,
            chave=chave,
            item=item,
            decidir=decidir,
            fechar=fechar,
            reabrir=reabrir,
            lote=lote,
        )

    return _novo


# --- matriz de papéis ---------------------------------------------------------------


@pytest.mark.parametrize("quem", ["admin", "gestor", "analista"])
def test_todos_justificam_desfazem_e_conferem(db_session, cenario, quem):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c")])

    justificar = c.decidir(quem, banco, "20.00", "justificada", "motivo")
    desfazer = c.decidir(quem, banco, "20.00", "justificativa_desfeita")
    conferir = c.decidir(quem, banco, "30.00", "conferida")
    desconferir = c.decidir(quem, banco, "30.00", "conferencia_desfeita")

    assert [r.status_code for r in (justificar, desfazer, conferir, desconferir)] == [200] * 4
    assert justificar.json()["situacao"] == "pendente"
    assert conferir.json()["situacao"] is None


@pytest.mark.parametrize(("quem", "esperado"), [("admin", 200), ("gestor", 200), ("analista", 403)])
@pytest.mark.parametrize("tipo", ["justificativa_aprovada", "justificativa_rejeitada"])
def test_quem_aprova_e_rejeita_uma_a_uma(db_session, cenario, quem, esperado, tipo):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "motivo")

    resposta = c.decidir(quem, banco, "20.00", tipo)

    assert resposta.status_code == esperado
    if esperado == 403:
        assert resposta.json() == SO_ADMIN_OU_GESTOR
        assert c.item(banco, "20.00")["decisao"]["situacao"] == "pendente"


@pytest.mark.parametrize(("quem", "esperado"), [("admin", 200), ("gestor", 200), ("analista", 403)])
@pytest.mark.parametrize("acao", ["aprovar", "rejeitar"])
def test_quem_aprova_e_rejeita_em_lote(db_session, cenario, quem, esperado, acao):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "motivo")

    resposta = c.lote(quem, acao=acao, competencia="2026-09")

    assert resposta.status_code == esperado
    if esperado == 403:
        assert resposta.json() == SO_ADMIN_OU_GESTOR
        assert c.listar(banco)["aguardando_aprovacao"] == 1
    else:
        assert resposta.json() == {"processadas": 1, "ignoradas": 0}


@pytest.mark.parametrize(
    ("quem", "fecha", "reabre"),
    [("admin", 201, 200), ("gestor", 201, 200), ("analista", 403, 403)],
)
def test_quem_fecha_e_reabre(db_session, cenario, quem, fecha, reabre):
    c = cenario()
    c.par([("10.00", "a")])

    fechar = c.fechar(quem)
    if fecha == 403:
        assert fechar.json() == SO_ADMIN_OU_GESTOR
        assert c.fechar("admin").status_code == 201
    reabrir = c.reabrir(quem)

    assert (fechar.status_code, reabrir.status_code) == (fecha, reabre)
    if reabre == 403:
        assert reabrir.json() == SO_ADMIN_OU_GESTOR
    listar = client.get("/fechamentos", headers=c.headers[quem])
    assert listar.status_code == 200
    assert listar.json()["itens"][0]["estado"] == ("fechado" if reabre == 403 else "reaberto")


# --- o fluxo do critério de aceite ----------------------------------------------------


def test_analista_justifica_e_o_gestor_aprova_uma_a_uma_e_o_mes_fecha(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])

    justificar = c.decidir("analista", banco, "20.00", "justificada", "pago em dinheiro")

    assert justificar.status_code == 200
    decisao = justificar.json()
    assert (decisao["tipo"], decisao["situacao"]) == ("justificada", "pendente")
    assert decisao["aprovada_por"] is None and decisao["aprovada_em"] is None
    assert c.listar(banco)["aguardando_aprovacao"] == 1
    assert c.fechar("gestor").json() == _aguardando(1)
    assert c.fechar("gestor", ressalva="fecha assim mesmo").json() == _aguardando(1)

    aprovar = c.decidir("gestor", banco, "20.00", "justificativa_aprovada")

    assert aprovar.status_code == 200
    aprovada = aprovar.json()
    assert (aprovada["tipo"], aprovada["texto"], aprovada["autor"]) == (
        "justificada",
        "pago em dinheiro",
        "Analista da Silva",
    )
    assert aprovada["situacao"] == "aprovada"
    assert aprovada["aprovada_por"] == "Gestor da Silva"
    assert aprovada["aprovada_em"] is not None
    item = c.item(banco, "20.00")
    assert item["decisao"] == aprovada
    assert [e["tipo"] for e in item["eventos"]] == ["justificada", "justificativa_aprovada"]
    assert all(e["situacao"] is None for e in item["eventos"])
    assert c.listar(banco)["aguardando_aprovacao"] == 0

    fechar = c.fechar("gestor")

    assert fechar.status_code == 201
    resumo = fechar.json()["resumo"]
    assert (resumo["justificadas"], resumo["aguardando_aprovacao"], resumo["pendentes"]) == (
        1,
        0,
        0,
    )


def test_gestor_aprova_em_lote_e_o_mes_fecha(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    c.decidir("analista", banco, "20.00", "justificada", "um")
    c.decidir("analista", banco, "30.00", "justificada", "dois")
    assert c.fechar().json() == _aguardando(2)

    lote = c.lote("gestor", acao="aprovar", competencia="2026-09")

    assert lote.json() == {"processadas": 2, "ignoradas": 0}
    assert c.item(banco, "30.00")["decisao"]["aprovada_por"] == "Gestor da Silva"
    assert c.fechar("gestor").status_code == 201


def test_rejeicao_volta_para_divergente_e_mostra_o_motivo(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "motivo fraco")

    rejeitar = c.decidir("gestor", banco, "20.00", "justificativa_rejeitada", "  falta o recibo ")

    assert rejeitar.status_code == 200
    assert rejeitar.json() is None
    item = c.item(banco, "20.00")
    assert item["decisao"] is None
    *_, rejeicao = item["eventos"]
    assert (rejeicao["tipo"], rejeicao["texto"], rejeicao["autor"]) == (
        "justificativa_rejeitada",
        "falta o recibo",
        "Gestor da Silva",
    )
    # Volta a ser divergência sem justificativa: a ressalva cobre.
    assert "sem justificativa" in c.fechar().json()["detail"]
    assert c.fechar(ressalva="vai sem o recibo").status_code == 201


def test_rejeicao_sem_motivo(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "motivo")

    assert c.decidir("admin", banco, "20.00", "justificativa_rejeitada").status_code == 200
    assert c.item(banco, "20.00")["eventos"][-1]["texto"] is None


def test_justificar_de_novo_depois_da_rejeicao_cria_outra_pendente(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "primeira")
    c.decidir("gestor", banco, "20.00", "justificativa_rejeitada", "não")

    nova = c.decidir("analista", banco, "20.00", "justificada", "segunda")

    assert (nova.json()["texto"], nova.json()["situacao"]) == ("segunda", "pendente")


def test_admin_sozinho_justifica_aprova_a_propria_e_fecha(db_session, cenario):
    c = cenario(papeis=("admin",))
    banco = c.par([("10.00", "a"), ("20.00", "b")])

    assert c.decidir("admin", banco, "20.00", "justificada", "minha").status_code == 200
    aprovar = c.decidir("admin", banco, "20.00", "justificativa_aprovada")

    assert aprovar.json()["autor"] == aprovar.json()["aprovada_por"] == "Ana Admin"
    assert c.fechar().status_code == 201


@pytest.mark.parametrize("quem", ["admin", "gestor"])
def test_quem_aprova_tambem_nasce_pendente_e_pode_aprovar_a_propria(db_session, cenario, quem):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])

    assert c.decidir(quem, banco, "20.00", "justificada", "x").json()["situacao"] == "pendente"
    assert c.decidir(quem, banco, "20.00", "justificativa_aprovada").status_code == 200


def test_justificar_de_novo_uma_aprovada_volta_a_ficar_pendente(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "velha")
    c.decidir("gestor", banco, "20.00", "justificativa_aprovada")

    nova = c.decidir("analista", banco, "20.00", "justificada", "texto novo")

    assert (nova.json()["texto"], nova.json()["situacao"]) == ("texto novo", "pendente")
    assert c.fechar().json() == _aguardando(1)


@pytest.mark.parametrize("aprovada", [True, False], ids=["aprovada", "pendente"])
def test_todos_desfazem_justificativa_aprovada_ou_pendente(db_session, cenario, aprovada):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("gestor", banco, "20.00", "justificada", "motivo")
    if aprovada:
        c.decidir("gestor", banco, "20.00", "justificativa_aprovada")

    desfazer = c.decidir("analista", banco, "20.00", "justificativa_desfeita")

    assert desfazer.status_code == 200
    assert c.item(banco, "20.00")["decisao"] is None


# --- validações uma a uma -------------------------------------------------------------


@pytest.mark.parametrize("tipo", ["justificativa_aprovada", "justificativa_rejeitada"])
def test_aprovar_ou_rejeitar_sem_justificativa_aguardando_e_422(db_session, cenario, tipo):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    c.decidir("analista", banco, "30.00", "conferida")
    c.decidir("analista", banco, "20.00", "justificada", "x")
    c.decidir("admin", banco, "20.00", "justificativa_aprovada")

    sem_decisao_ou_conferida = c.decidir("admin", banco, "30.00", tipo)
    ja_aprovada = c.decidir("admin", banco, "20.00", tipo)

    assert sem_decisao_ou_conferida.status_code == ja_aprovada.status_code == 422
    assert sem_decisao_ou_conferida.json() == ja_aprovada.json() == SEM_AGUARDANDO


def test_aprovacao_com_texto_e_motivo_grande_demais_sao_422(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "x")

    com_texto = c.decidir("admin", banco, "20.00", "justificativa_aprovada", "ok")
    motivo_grande = c.decidir("admin", banco, "20.00", "justificativa_rejeitada", "x" * 1001)

    assert com_texto.json() == {"detail": "A aprovação não tem texto."}
    assert motivo_grande.status_code == 422
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "pendente"


def test_aprovar_ou_rejeitar_em_mes_fechado_e_409(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    assert c.fechar(ressalva="fecha").status_code == 201
    # Uma justificativa pendente gravada direto: a API não justifica em mês fechado.
    usuario = c.usuarios["analista"]
    db_session.add(
        DecisaoLinha(
            empresa_id=c.empresa_id,
            extrato_banco_id=banco.id,
            chave=c.chave(banco, "20.00"),
            tipo="justificada",
            texto="direto no banco",
            usuario_id=usuario.id,
            autor_nome=usuario.nome,
            rodada=1,
            extrato_sistema_id=c.item(banco, "20.00")["extrato_sistema_id"],
        )
    )
    db_session.commit()

    aprovar = c.decidir("admin", banco, "20.00", "justificativa_aprovada")
    lote = c.lote("admin", acao="rejeitar", competencia="2026-09")
    lote_itens = c.lote(
        "admin",
        acao="aprovar",
        itens=[{"extrato_banco_id": str(banco.id), "chave": c.chave(banco, "20.00")}],
    )

    assert aprovar.status_code == lote.status_code == lote_itens.status_code == 409
    assert (
        aprovar.json()
        == lote.json()
        == {"detail": "A competência 2026-09 está fechada. Reabra o fechamento para alterar."}
    )
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "pendente"


# --- lote -----------------------------------------------------------------------------


def test_lote_por_competencia_pega_so_os_pares_da_competencia(db_session, cenario):
    c = cenario()
    setembro_1 = c.par([("10.00", "a"), ("20.00", "b")])
    setembro_2 = c.par([("10.00", "a"), ("40.00", "d")])
    outubro = c.par([("10.00", "a"), ("50.00", "e")], periodo=OUTUBRO)
    for banco, valor in ((setembro_1, "20.00"), (setembro_2, "40.00"), (outubro, "50.00")):
        c.decidir("analista", banco, valor, "justificada", "x")

    resposta = c.lote("gestor", acao="rejeitar", motivo="  sem comprovante ", competencia="2026-09")

    assert resposta.json() == {"processadas": 2, "ignoradas": 0}
    assert c.item(setembro_1, "20.00")["eventos"][-1]["texto"] == "sem comprovante"
    assert c.item(setembro_2, "40.00")["decisao"] is None
    assert c.item(outubro, "50.00")["decisao"]["situacao"] == "pendente"


def test_lote_por_competencia_usa_so_a_rodada_mais_recente(
    db_session, cenario, criar_extrato_da_empresa, inserir_lancamentos
):
    """A justificativa vale para a linha em qualquer rodada; o lote olha as
    linhas da rodada mais recente, como o fechamento."""
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "x")
    # Rodada 2: o extrato do sistema novo traz o 20.00, que deixa de divergir.
    sistema_2 = criar_extrato_da_empresa(c.empresa_id, "sistema")
    inserir_lancamentos(sistema_2, [("10.00", "a"), ("20.00", "b")])
    sistema_2.periodo_inicio, sistema_2.periodo_fim = SETEMBRO
    db_session.commit()
    client.post(
        "/conciliacoes",
        headers=c.headers["admin"],
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema_2.id)},
    )

    assert c.lote("admin", acao="aprovar", competencia="2026-09").json() == {
        "processadas": 0,
        "ignoradas": 0,
    }
    assert c.fechar().status_code == 201


def test_lote_por_itens_ignora_o_que_nao_esta_aguardando(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c"), ("40.00", "d")])
    c.decidir("analista", banco, "20.00", "justificada", "x")
    c.decidir("analista", banco, "30.00", "justificada", "y")
    c.decidir("admin", banco, "30.00", "justificativa_aprovada")

    def item(valor_ou_chave, extrato=banco):
        chave = valor_ou_chave if len(valor_ou_chave) > 10 else c.chave(banco, valor_ou_chave)
        return {"extrato_banco_id": str(extrato.id), "chave": chave}

    resposta = c.lote(
        "gestor",
        acao="aprovar",
        itens=[
            item("20.00"),  # aguardando: processa
            item("20.00"),  # repetido
            item("30.00"),  # já aprovada
            item("40.00"),  # sem justificativa
            item("10.00"),  # não diverge
            item("f" * 64),  # chave que não existe
            {"extrato_banco_id": str(uuid.uuid4()), "chave": "x" * 64},  # extrato inexistente
        ],
    )

    assert resposta.json() == {"processadas": 1, "ignoradas": 6}
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "aprovada"


@pytest.mark.parametrize(
    "corpo",
    [
        {"acao": "aprovar"},
        {
            "acao": "aprovar",
            "competencia": "2026-09",
            "itens": [{"extrato_banco_id": str(uuid.uuid4()), "chave": "x"}],
        },
        {"acao": "aprovar", "competencia": "2026-13"},
        {"acao": "aprovar", "itens": []},
        {"acao": "aprovar", "motivo": "não cabe", "competencia": "2026-09"},
        {"acao": "aprovar_tudo", "competencia": "2026-09"},
        {"acao": "rejeitar", "motivo": "x" * 1001, "competencia": "2026-09"},
        {
            "acao": "aprovar",
            "itens": [{"extrato_banco_id": str(uuid.uuid4()), "chave": "x"}] * (MAXIMO_ITENS + 1),
        },
    ],
    ids=[
        "nenhum",
        "os_dois",
        "competencia_invalida",
        "itens_vazio",
        "motivo_na_aprovacao",
        "acao_invalida",
        "motivo_grande",
        "itens_demais",
    ],
)
def test_lote_com_corpo_invalido_e_422(db_session, cenario, corpo):
    c = cenario()

    assert c.lote("admin", **corpo).status_code == 422


def test_lote_com_500_itens_e_aceito(db_session, cenario):
    c = cenario()
    itens = [{"extrato_banco_id": str(uuid.uuid4()), "chave": "x"}] * MAXIMO_ITENS

    assert c.lote("admin", acao="aprovar", itens=itens).json() == {
        "processadas": 0,
        "ignoradas": MAXIMO_ITENS,
    }


# --- acesso cruzado -------------------------------------------------------------------


def test_lote_respeita_a_empresa_ativa_e_a_conta(db_session, cenario, criar_empresa):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "x")
    outra_da_conta = criar_empresa("Cliente 2", conta_id=c.usuarios["admin"].conta_id)
    na_outra_empresa = {**c.headers["admin"], CABECALHO_EMPRESA: str(outra_da_conta.id)}
    item = {"extrato_banco_id": str(banco.id), "chave": c.chave(banco, "20.00")}

    por_itens = client.post(
        "/justificativas/lote",
        headers=na_outra_empresa,
        json={"acao": "aprovar", "itens": [item]},
    )
    por_competencia = client.post(
        "/justificativas/lote",
        headers=na_outra_empresa,
        json={"acao": "aprovar", "competencia": "2026-09"},
    )
    outra_conta = cenario()
    de_outra_conta = client.post(
        "/justificativas/lote",
        headers={**outra_conta.headers["admin"], CABECALHO_EMPRESA: str(c.empresa_id)},
        json={"acao": "aprovar", "itens": [item]},
    )
    itens_de_outra_conta = outra_conta.lote("admin", acao="aprovar", itens=[item])

    assert por_itens.json() == {"processadas": 0, "ignoradas": 1}
    assert por_competencia.json() == {"processadas": 0, "ignoradas": 0}
    assert de_outra_conta.status_code == 404
    assert de_outra_conta.json() == {"detail": "Empresa não encontrada."}
    assert itens_de_outra_conta.json() == {"processadas": 0, "ignoradas": 1}
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "pendente"


def test_aprovar_uma_a_uma_com_outra_empresa_ativa_e_404(db_session, cenario, criar_empresa):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])
    c.decidir("analista", banco, "20.00", "justificada", "x")
    outra = criar_empresa("Cliente 2", conta_id=c.usuarios["admin"].conta_id)

    resposta = client.post(
        f"/conciliacoes/{banco.id}/decisoes",
        headers={**c.headers["admin"], CABECALHO_EMPRESA: str(outra.id)},
        json={"chave": c.chave(banco, "20.00"), "tipo": "justificativa_aprovada"},
    )

    assert resposta.status_code == 404
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "pendente"


# --- contagens e resumos --------------------------------------------------------------


def test_pendente_conta_a_parte_nas_execucoes_e_no_resumo(db_session, cenario):
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c"), ("40.00", "d")])
    c.decidir("analista", banco, "20.00", "justificada", "pendente")
    c.decidir("analista", banco, "30.00", "justificada", "aprovada")
    c.decidir("gestor", banco, "30.00", "justificativa_aprovada")

    (execucao,) = client.get(
        "/execucoes", headers=c.headers["analista"], params={"extrato_banco_id": str(banco.id)}
    ).json()["itens"]

    assert execucao["contagens"]["justificadas"] == 1
    assert execucao["contagens"]["aguardando_aprovacao"] == 1
    assert c.listar(banco)["aguardando_aprovacao"] == 1

    c.lote("admin", acao="aprovar", competencia="2026-09")
    resumo = c.fechar(ressalva="o 40 fica em aberto").json()["resumo"]
    assert (resumo["justificadas"], resumo["aguardando_aprovacao"], resumo["pendentes"]) == (
        2,
        0,
        1,
    )


def test_resumo_congelado_antigo_sai_com_aguardando_zero(db_session, cenario):
    c = cenario()
    usuario = c.usuarios["admin"]
    db_session.add(
        Fechamento(
            empresa_id=c.empresa_id,
            competencia="2026-08",
            resumo={
                "pares": [],
                "contagens": dict.fromkeys(
                    [
                        "total",
                        "match_exato",
                        "match_tolerancia",
                        "duplicado",
                        "sem_correspondencia",
                        "tarifa_bancaria",
                        "divergente_valor",
                        "divergente_data",
                    ],
                    0,
                ),
                "justificadas": 3,
                "pendentes": 0,
                "linhas_nao_lidas": 0,
                "valor_em_aberto": "0.00",
            },
            fechado_por_id=usuario.id,
            fechado_por_nome=usuario.nome,
        )
    )
    db_session.commit()

    (item,) = client.get("/fechamentos", headers=c.headers["analista"]).json()["itens"]

    assert (item["resumo"]["justificadas"], item["resumo"]["aguardando_aprovacao"]) == (3, 0)
    assert (
        db_session.scalar(select(Fechamento.resumo).where(Fechamento.competencia == "2026-08"))[
            "justificadas"
        ]
        == 3
    )


# --- flag desligada (o padrão até o front aprovar) ---------------------------------


def test_flag_desligada_justificativa_nasce_aprovada_e_o_mes_fecha(
    db_session, cenario, monkeypatch
):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b")])

    justificar = c.decidir("analista", banco, "20.00", "justificada", "motivo")

    decisao = justificar.json()
    assert (decisao["situacao"], decisao["aprovada_por"]) == ("aprovada", "Analista da Silva")
    assert [e["tipo"] for e in c.item(banco, "20.00")["eventos"]] == [
        "justificada",
        "justificativa_aprovada",
    ]
    assert c.listar(banco)["aguardando_aprovacao"] == 0
    assert c.lote("gestor", acao="aprovar", competencia="2026-09").json() == {
        "processadas": 0,
        "ignoradas": 0,
    }
    assert c.decidir("admin", banco, "20.00", "justificativa_aprovada").json() == SEM_AGUARDANDO
    for resposta in (
        c.decidir("analista", banco, "20.00", "justificativa_rejeitada"),
        c.lote("analista", acao="aprovar", competencia="2026-09"),
        c.fechar("analista"),
    ):
        assert resposta.status_code == 403
        assert resposta.json() == SO_ADMIN_OU_GESTOR
    assert c.fechar("gestor").status_code == 201
    assert c.reabrir("analista").json() == SO_ADMIN_OU_GESTOR


def test_ligar_a_flag_nao_muda_o_que_ja_foi_aprovado(db_session, cenario, monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par([("10.00", "a"), ("20.00", "b"), ("30.00", "c")])
    c.decidir("analista", banco, "20.00", "justificada", "antes")

    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)
    c.decidir("analista", banco, "30.00", "justificada", "depois")

    assert c.item(banco, "20.00")["decisao"]["situacao"] == "aprovada"
    assert c.item(banco, "30.00")["decisao"]["situacao"] == "pendente"
    assert c.fechar().json() == _aguardando(1)
