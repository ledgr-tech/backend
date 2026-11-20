"""Justificativa em lote (issue #237): `POST /conciliacoes/{extrato_id}/decisoes/lote`,
o `lote_id` e o `lote_tamanho` na decisão de `GET /conciliacoes/{extrato_id}` e
a forma `lote_id` de `POST /justificativas/lote`.

Cenário: uma conta com admin, gestor e analista, uma empresa e pares
conciliados pela API em setembro de 2026. O par padrão tem um match exato
(10.00), três linhas só do banco (20.00, 30.00 e 40.00) e uma só do sistema
(99.00), as quatro divergentes. A flag de aprovação fica ligada, salvo nos
testes que dizem o contrário.
"""

import json
import logging
import threading
import time
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import cabecalhos, empresa_unica
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.api import conciliacoes
from app.core.config import settings
from app.core.database import engine
from app.models import DecisaoLinha
from app.services.decisoes import MAXIMO_LINHAS_POR_LOTE
from app.services.travas import chave_do_extrato_banco, chave_do_mes
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
TEXTO = "Tarifas do pacote cobradas em duas vezes"
BANCO_PADRAO = (("10.00", "a"), ("20.00", "b"), ("30.00", "c"), ("40.00", "d"))
SISTEMA_PADRAO = (("10.00", "a"), ("99.00", "so sistema"))
MES_FECHADO = {"detail": "A competência 2026-09 está fechada. Reabra o fechamento para alterar."}
NADA_IGNORADO = {"nao_encontrada": 0, "nao_diverge": 0, "repetida": 0, "aprovada": 0}


@pytest.fixture(autouse=True)
def _aprovacao_ligada(monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", True)


@pytest.fixture
def cenario(db_session, criar_usuario, criar_extrato_da_empresa, inserir_lancamentos, gerar_token):
    """`cenario()` monta a conta; `par()` concilia um par pela API."""

    def _novo():
        admin = criar_usuario(nome="Ana Admin")
        usuarios = {"admin": admin}
        for papel in ("gestor", "analista"):
            usuarios[papel] = criar_usuario(
                conta_id=admin.conta_id, nome=f"{papel.title()} da Silva", papel=papel
            )
        empresa_id = empresa_unica(db_session, admin.conta_id)
        headers = {
            papel: cabecalhos(gerar_token(u.conta_id, usuario_id=u.id), empresa_id)
            for papel, u in usuarios.items()
        }

        def extrato(origem, itens, periodo=SETEMBRO):
            novo = criar_extrato_da_empresa(empresa_id, origem)
            inserir_lancamentos(novo, list(itens))
            novo.periodo_inicio, novo.periodo_fim = periodo
            db_session.commit()
            return novo

        def par(itens_banco=BANCO_PADRAO, itens_sistema=SISTEMA_PADRAO):
            banco = extrato("banco", itens_banco)
            sistema = extrato("sistema", itens_sistema)
            resposta = client.post(
                "/conciliacoes",
                headers=headers["admin"],
                json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
            )
            assert resposta.status_code == 201, resposta.text
            return banco

        def listar(banco, quem="admin", **params):
            resposta = client.get(f"/conciliacoes/{banco.id}", headers=headers[quem], params=params)
            assert resposta.status_code == 200, resposta.text
            return resposta.json()

        def item(banco, valor):
            """O item pelo valor do banco, ou pelo do sistema nas linhas só do sistema."""
            for i in listar(banco, limit=1000)["itens"]:
                lado = i["lancamento_banco"] or i["lancamento_sistema"]
                if lado["valor"] == valor:
                    return i
            raise AssertionError(f"linha {valor} não encontrada")

        def chave(banco, valor):
            return item(banco, valor)["chave"]

        def justificar(banco, chaves, quem="analista", texto=TEXTO, **extra):
            return client.post(
                f"/conciliacoes/{banco.id}/decisoes/lote",
                headers=headers[quem],
                json={"tipo": "justificada", "texto": texto, "chaves": chaves, **extra},
            )

        def decidir(quem, banco, valor, tipo, texto=None):
            return client.post(
                f"/conciliacoes/{banco.id}/decisoes",
                headers=headers[quem],
                json={"chave": chave(banco, valor), "tipo": tipo, "texto": texto},
            )

        def aprovar_lote(quem="gestor", **corpo):
            return client.post(
                "/justificativas/lote", headers=headers[quem], json={"acao": "aprovar", **corpo}
            )

        def fechar(ressalva="fecha com ressalva"):
            return client.post(
                "/fechamentos",
                headers=headers["admin"],
                json={"competencia": "2026-09", "ressalva": ressalva},
            )

        return SimpleNamespace(
            usuarios=usuarios,
            headers=headers,
            empresa_id=empresa_id,
            extrato=extrato,
            par=par,
            listar=listar,
            item=item,
            chave=chave,
            justificar=justificar,
            decidir=decidir,
            aprovar_lote=aprovar_lote,
            fechar=fechar,
        )

    return _novo


def _eventos(db_session, banco):
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(DecisaoLinha)
            .where(DecisaoLinha.extrato_banco_id == banco.id)
            .order_by(DecisaoLinha.em, DecisaoLinha.id)
        )
    )


def _processadas(n, lote_id, **ignoradas):
    por_motivo = {**NADA_IGNORADO, **ignoradas}
    return {
        "processadas": n,
        "ignoradas": sum(por_motivo.values()),
        "ignoradas_por_motivo": por_motivo,
        "lote_id": lote_id,
    }


# --- o caminho feliz --------------------------------------------------------------------


def test_justifica_tres_linhas_de_uma_vez(db_session, cenario):
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, valor) for valor in ("20.00", "30.00", "99.00")]

    resposta = c.justificar(banco, chaves)

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    lote_id = corpo["lote_id"]
    assert uuid.UUID(lote_id)
    assert corpo == _processadas(3, lote_id)

    lista = c.listar(banco)
    assert lista["aguardando_aprovacao"] == 3
    for valor in ("20.00", "30.00", "99.00"):
        item = next(
            i
            for i in lista["itens"]
            if (i["lancamento_banco"] or i["lancamento_sistema"])["valor"] == valor
        )
        decisao = item["decisao"]
        assert decisao["tipo"] == "justificada"
        assert decisao["texto"] == TEXTO
        assert decisao["situacao"] == "pendente"
        assert decisao["autor"] == "Analista da Silva"
        assert (decisao["lote_id"], decisao["lote_tamanho"]) == (lote_id, 3)
        # O lote é dado do evento; o tamanho fica só na decisão.
        assert [(e["tipo"], e["lote_id"]) for e in item["eventos"]] == [("justificada", lote_id)]
        assert "lote_tamanho" not in item["eventos"][0]
    assert c.item(banco, "40.00")["decisao"] is None

    eventos = _eventos(db_session, banco)
    assert len(eventos) == 3
    assert {e.lote_id for e in eventos} == {uuid.UUID(lote_id)}
    assert {e.usuario_id for e in eventos} == {c.usuarios["analista"].id}
    assert {(e.rodada, e.extrato_sistema_id) for e in eventos} == {
        (1, uuid.UUID(c.item(banco, "20.00")["extrato_sistema_id"]))
    }


def test_justificativa_de_uma_linha_fica_sem_lote(db_session, cenario):
    c = cenario()
    banco = c.par()

    resposta = c.decidir("analista", banco, "20.00", "justificada", "uma só")

    assert (resposta.json()["lote_id"], resposta.json()["lote_tamanho"]) == (None, None)
    decisao = c.item(banco, "20.00")["decisao"]
    assert (decisao["lote_id"], decisao["lote_tamanho"]) == (None, None)
    assert c.item(banco, "20.00")["eventos"][0]["lote_id"] is None


def test_texto_e_aparado(db_session, cenario):
    c = cenario()
    banco = c.par()

    assert c.justificar(banco, [c.chave(banco, "20.00")], texto=f"  {TEXTO}\n").status_code == 200

    assert c.item(banco, "20.00")["decisao"]["texto"] == TEXTO


@pytest.mark.parametrize("quem", ["admin", "gestor", "analista"])
def test_qualquer_papel_justifica_em_lote(db_session, cenario, quem):
    c = cenario()
    banco = c.par()

    resposta = c.justificar(banco, [c.chave(banco, "20.00"), c.chave(banco, "30.00")], quem=quem)

    assert resposta.status_code == 200
    assert resposta.json()["processadas"] == 2


# --- linhas ignoradas -------------------------------------------------------------------


def _chave_de_outra_conciliacao(c):
    outro = c.par([("10.00", "a"), ("77.00", "outra")])
    return c.chave(outro, "77.00")


MOTIVOS = {
    "nao_encontrada": lambda c, banco: [
        "f" * 64,
        str(uuid.uuid4()),
        _chave_de_outra_conciliacao(c),
        c.chave(banco, "20.00").upper(),  # UUID fora da forma canônica
    ],
    "nao_diverge": lambda c, banco: [c.chave(banco, "10.00")],
    "repetida": lambda c, banco: None,  # montada no teste: precisa de uma válida antes
    "aprovada": lambda c, banco: None,
}


def _preparar(c, banco, motivo):
    """As chaves que caem no motivo, sem nenhuma válida (salvo a repetida, cuja
    primeira ocorrência conta como processada)."""
    if motivo == "repetida":
        chave = c.chave(banco, "20.00")
        return [chave, chave, chave], 1
    if motivo == "aprovada":
        assert c.decidir("analista", banco, "30.00", "justificada", "antes").status_code == 200
        assert c.decidir("gestor", banco, "30.00", "justificativa_aprovada").status_code == 200
        return [c.chave(banco, "30.00")], 0
    return MOTIVOS[motivo](c, banco), 0


@pytest.mark.parametrize("motivo", list(MOTIVOS))
def test_cada_motivo_de_ignorada_sozinho(db_session, cenario, motivo):
    c = cenario()
    banco = c.par()
    chaves, validas = _preparar(c, banco, motivo)
    antes = len(_eventos(db_session, banco))

    resposta = c.justificar(banco, chaves)

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo == _processadas(validas, corpo["lote_id"], **{motivo: len(chaves) - validas})
    assert (corpo["lote_id"] is None) == (validas == 0)
    assert len(_eventos(db_session, banco)) == antes + validas


def test_motivos_misturados_com_validas(db_session, cenario):
    c = cenario()
    banco = c.par()
    assert c.decidir("analista", banco, "30.00", "justificada", "antes").status_code == 200
    assert c.decidir("gestor", banco, "30.00", "justificativa_aprovada").status_code == 200
    vinte, quarenta, noventa_e_nove = (c.chave(banco, v) for v in ("20.00", "40.00", "99.00"))

    resposta = c.justificar(
        banco,
        [
            vinte,  # válida
            "f" * 64,  # não está na rodada
            c.chave(banco, "10.00"),  # não diverge
            vinte,  # repetida
            c.chave(banco, "30.00"),  # aprovada
            quarenta,  # válida
            noventa_e_nove,  # válida
            "f" * 64,  # repetida (a primeira foi não encontrada)
        ],
    )

    corpo = resposta.json()
    assert corpo == _processadas(
        3, corpo["lote_id"], nao_encontrada=1, nao_diverge=1, repetida=2, aprovada=1
    )
    for valor in ("20.00", "40.00", "99.00"):
        decisao = c.item(banco, valor)["decisao"]
        assert (decisao["lote_id"], decisao["lote_tamanho"]) == (corpo["lote_id"], 3)
    # A aprovada continua a de antes, aprovada.
    aprovada = c.item(banco, "30.00")["decisao"]
    assert (aprovada["texto"], aprovada["situacao"], aprovada["lote_id"]) == (
        "antes",
        "aprovada",
        None,
    )


def test_linha_de_rodada_anterior_que_saiu_e_nao_encontrada(db_session, cenario):
    c = cenario()
    banco = c.par()
    so_na_v1 = c.chave(banco, "99.00")
    v2 = c.extrato("sistema", [("10.00", "a"), ("55.00", "so na v2")])
    resposta = client.post(
        "/conciliacoes",
        headers=c.headers["admin"],
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(v2.id)},
    )
    assert resposta.status_code == 201

    corpo = c.justificar(banco, [so_na_v1, c.chave(banco, "55.00")]).json()

    assert corpo == _processadas(1, corpo["lote_id"], nao_encontrada=1)
    assert _eventos(db_session, banco)[0].rodada == 2


def test_linha_com_justificativa_pendente_recebe_a_nova(db_session, cenario):
    c = cenario()
    banco = c.par()
    assert c.decidir("gestor", banco, "20.00", "justificada", "antes").status_code == 200

    corpo = c.justificar(banco, [c.chave(banco, "20.00"), c.chave(banco, "30.00")]).json()

    assert corpo == _processadas(2, corpo["lote_id"])
    decisao = c.item(banco, "20.00")["decisao"]
    assert (decisao["texto"], decisao["situacao"], decisao["lote_id"]) == (
        TEXTO,
        "pendente",
        corpo["lote_id"],
    )
    assert [e["tipo"] for e in c.item(banco, "20.00")["eventos"]] == ["justificada"] * 2


def test_linha_conferida_recebe_a_justificativa(db_session, cenario):
    c = cenario()
    banco = c.par()
    assert c.decidir("analista", banco, "20.00", "conferida").status_code == 200

    assert c.justificar(banco, [c.chave(banco, "20.00")]).json()["processadas"] == 1

    assert c.item(banco, "20.00")["decisao"]["tipo"] == "justificada"


# --- flag de aprovação desligada --------------------------------------------------------


def test_flag_desligada_cada_justificativa_nasce_aprovada(db_session, cenario, monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, valor) for valor in ("20.00", "30.00", "99.00")]

    corpo = c.justificar(banco, chaves).json()

    assert corpo == _processadas(3, corpo["lote_id"])
    lista = c.listar(banco)
    assert lista["aguardando_aprovacao"] == 0
    for valor in ("20.00", "30.00", "99.00"):
        item = c.item(banco, valor)
        decisao = item["decisao"]
        assert (decisao["situacao"], decisao["aprovada_por"]) == ("aprovada", "Analista da Silva")
        assert (decisao["lote_id"], decisao["lote_tamanho"]) == (corpo["lote_id"], 3)
        # A aprovação junto não leva o lote, e vem um microssegundo depois.
        assert [(e["tipo"], e["lote_id"]) for e in item["eventos"]] == [
            ("justificada", corpo["lote_id"]),
            ("justificativa_aprovada", None),
        ]
    eventos = _eventos(db_session, banco)
    por_chave = {}
    for evento in eventos:
        por_chave.setdefault(evento.chave, []).append(evento)
    for justificada, aprovada in por_chave.values():
        assert (aprovada.em - justificada.em).total_seconds() == pytest.approx(1e-6)


def test_flag_desligada_linha_aprovada_recebe_a_nova(db_session, cenario, monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par()
    assert c.decidir("analista", banco, "20.00", "justificada", "antes").status_code == 200
    assert c.item(banco, "20.00")["decisao"]["situacao"] == "aprovada"

    corpo = c.justificar(banco, [c.chave(banco, "20.00")]).json()

    assert corpo == _processadas(1, corpo["lote_id"])
    decisao = c.item(banco, "20.00")["decisao"]
    assert (decisao["texto"], decisao["situacao"], decisao["lote_id"]) == (
        TEXTO,
        "aprovada",
        corpo["lote_id"],
    )


# --- erros do pedido inteiro ------------------------------------------------------------


def test_extrato_de_outra_empresa_e_404(
    db_session, cenario, criar_empresa, criar_extrato_da_empresa
):
    c = cenario()
    outro = criar_extrato_da_empresa(criar_empresa().id, "banco")

    resposta = c.justificar(outro, ["f" * 64])

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Extrato não encontrado."}


def test_extrato_do_sistema_e_422(db_session, cenario):
    c = cenario()
    c.par()
    sistema = c.extrato("sistema", [("1.00", "x")])

    resposta = c.justificar(sistema, ["f" * 64])

    assert resposta.status_code == 422
    assert resposta.json() == {
        "detail": "A consulta deve usar o extrato do banco, não o do sistema."
    }


def test_extrato_nao_conciliado_e_404(db_session, cenario):
    c = cenario()
    banco = c.extrato("banco", [("1.00", "x")])

    resposta = c.justificar(banco, ["f" * 64])

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "Este extrato ainda não foi conciliado."}


def test_mes_fechado_e_409_e_nada_e_gravado(db_session, cenario):
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, "20.00"), c.chave(banco, "30.00")]
    assert c.fechar().status_code == 201

    resposta = c.justificar(banco, chaves)

    assert resposta.status_code == 409
    assert resposta.json() == MES_FECHADO
    assert _eventos(db_session, banco) == []


@pytest.mark.parametrize(
    ("texto", "detalhe"),
    [
        (None, "A justificativa precisa de um texto."),
        ("", "A justificativa precisa de um texto."),
        ("   \n ", "A justificativa precisa de um texto."),
        ("x" * 1001, "A justificativa pode ter até 1000 caracteres."),
    ],
    ids=["nulo", "vazio", "so_espacos", "acima_do_limite"],
)
def test_texto_invalido_e_422_com_a_mesma_mensagem(db_session, cenario, texto, detalhe):
    c = cenario()
    banco = c.par()

    resposta = c.justificar(banco, [c.chave(banco, "20.00")], texto=texto)

    assert resposta.status_code == 422
    assert resposta.json() == {"detail": detalhe}
    assert _eventos(db_session, banco) == []


def test_texto_de_1000_caracteres_e_aceito(db_session, cenario):
    c = cenario()
    banco = c.par()

    assert c.justificar(banco, [c.chave(banco, "20.00")], texto="x" * 1000).status_code == 200


@pytest.mark.parametrize(
    "corpo",
    [
        {"tipo": "justificada", "texto": TEXTO, "chaves": []},
        {
            "tipo": "justificada",
            "texto": TEXTO,
            "chaves": ["f" * 64] * (MAXIMO_LINHAS_POR_LOTE + 1),
        },
        {"tipo": "justificada", "texto": TEXTO, "chaves": [""]},
        {"tipo": "justificada", "texto": TEXTO, "chaves": ["f" * 65]},
        {"tipo": "justificada", "texto": TEXTO},
        {"tipo": "conferida", "chaves": ["f" * 64]},
        {"texto": TEXTO, "chaves": ["f" * 64]},
    ],
    ids=[
        "zero_chaves",
        "501_chaves",
        "chave_vazia",
        "chave_grande",
        "sem_chaves",
        "conferida",
        "sem_tipo",
    ],
)
def test_corpo_invalido_e_422(db_session, cenario, corpo):
    c = cenario()
    banco = c.par()

    resposta = client.post(
        f"/conciliacoes/{banco.id}/decisoes/lote", headers=c.headers["analista"], json=corpo
    )

    assert resposta.status_code == 422
    assert _eventos(db_session, banco) == []


def test_500_chaves_sao_aceitas_e_todas_ignoradas_e_200(db_session, cenario):
    c = cenario()
    banco = c.par()

    resposta = c.justificar(banco, [f"{indice:064x}" for indice in range(MAXIMO_LINHAS_POR_LOTE)])

    assert resposta.status_code == 200
    assert resposta.json() == _processadas(0, None, nao_encontrada=MAXIMO_LINHAS_POR_LOTE)


def test_sem_token_e_401(db_session, cenario):
    c = cenario()
    banco = c.par()

    resposta = client.post(
        f"/conciliacoes/{banco.id}/decisoes/lote",
        json={"tipo": "justificada", "texto": TEXTO, "chaves": ["x"]},
    )

    assert resposta.status_code == 401


# --- transação e concorrência -----------------------------------------------------------


def test_falha_no_meio_nao_deixa_nenhum_evento(db_session, cenario, monkeypatch):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, valor) for valor in ("20.00", "30.00", "40.00")]

    def _falhar_depois_de_gravar(db, justificativas):
        db.flush()  # as justificativas já foram ao banco, na transação
        assert len(justificativas) == 3
        raise RuntimeError("falha forçada no meio do lote")

    monkeypatch.setattr(conciliacoes, "_aprovar_junto", _falhar_depois_de_gravar)
    sem_excecao = TestClient(app, raise_server_exceptions=False)

    resposta = sem_excecao.post(
        f"/conciliacoes/{banco.id}/decisoes/lote",
        headers=c.headers["analista"],
        json={"tipo": "justificada", "texto": TEXTO, "chaves": chaves},
    )

    assert resposta.status_code == 500
    assert _eventos(db_session, banco) == []


def _segurar(chave):
    """Conexão separada segurando a trava exclusiva até `soltar()`."""
    conexao = engine.connect()
    transacao = conexao.begin()
    conexao.execute(text("SELECT pg_advisory_xact_lock(:chave)"), {"chave": chave})

    def soltar():
        transacao.rollback()
        conexao.close()

    return soltar


def _em_segundo_plano(funcao):
    resultado = {}

    def _rodar():
        resultado["resposta"] = funcao()

    thread = threading.Thread(target=_rodar)
    thread.start()
    return thread, resultado


def _esperando(chave, *funcoes):
    """Roda cada função em segundo plano, na ordem, com a trava segura; confere
    que todas esperam e solta. Devolve as respostas na ordem das funções."""
    soltar = _segurar(chave)
    try:
        execucoes = []
        for funcao in funcoes:
            execucoes.append(_em_segundo_plano(funcao))
            time.sleep(0.3)  # a próxima entra na fila depois desta
        assert all(thread.is_alive() for thread, _ in execucoes), "deveria esperar a trava"
    finally:
        soltar()
    for thread, _ in execucoes:
        thread.join(timeout=15)
        assert not thread.is_alive()
    return [resultado["resposta"] for _, resultado in execucoes]


def test_dois_lotes_sobre_as_mesmas_linhas_ao_mesmo_tempo(db_session, cenario):
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, valor) for valor in ("20.00", "30.00", "40.00", "99.00")]

    primeira, segunda = _esperando(
        chave_do_extrato_banco(c.empresa_id, banco.id),
        lambda: c.justificar(banco, chaves, quem="analista", texto="primeiro"),
        lambda: c.justificar(banco, list(reversed(chaves)), quem="gestor", texto="segundo"),
    )

    assert primeira.status_code == segunda.status_code == 200
    assert primeira.json()["processadas"] == segunda.json()["processadas"] == 4
    lotes = {primeira.json()["lote_id"], segunda.json()["lote_id"]}
    assert len(lotes) == 2
    # Um lote inteiro depois do outro, nunca intercalados: as quatro linhas
    # ficam com o mesmo lote em vigor, o de quem gravou por último.
    decisoes_em_vigor = [c.item(banco, v)["decisao"] for v in ("20.00", "30.00", "40.00", "99.00")]
    assert len({d["lote_id"] for d in decisoes_em_vigor}) == 1
    assert {d["lote_id"] for d in decisoes_em_vigor} <= lotes
    assert {d["lote_tamanho"] for d in decisoes_em_vigor} == {4}
    assert {d["situacao"] for d in decisoes_em_vigor} == {"pendente"}
    eventos = _eventos(db_session, banco)
    assert len(eventos) == 8
    # Os eventos de cada lote são contíguos na ordem por `em`.
    assert len({e.lote_id for e in eventos[:4]}) == len({e.lote_id for e in eventos[4:]}) == 1


def test_lote_no_instante_em_que_o_mes_fecha_e_409_sem_gravar(db_session, cenario):
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, "20.00"), c.chave(banco, "30.00")]

    # O fechamento entra na fila da trava do mês antes do lote; quando a trava
    # é solta, ele fecha, e o lote, que esperava, encontra o mês fechado.
    fechamento, lote = _esperando(
        chave_do_mes(c.empresa_id, "2026-09"),
        lambda: c.fechar(),
        lambda: c.justificar(banco, chaves),
    )

    assert fechamento.status_code == 201, fechamento.text
    assert lote.status_code == 409
    assert lote.json() == MES_FECHADO
    assert _eventos(db_session, banco) == []


def test_lote_que_chega_antes_do_fechamento_grava_e_o_fechamento_espera(
    db_session, cenario, monkeypatch
):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", False)
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, valor) for valor in ("20.00", "30.00", "40.00", "99.00")]

    lote, fechamento = _esperando(
        chave_do_mes(c.empresa_id, "2026-09"),
        lambda: c.justificar(banco, chaves),
        lambda: c.fechar(ressalva=None),
    )

    # O lote gravou antes, então o mês fecha sem ressalva: tudo justificado.
    assert lote.status_code == 200 and lote.json()["processadas"] == 4
    assert fechamento.status_code == 201, fechamento.text


# --- número de consultas ----------------------------------------------------------------


def _contar(funcao):
    contadas = []

    def _registrar(conn, cursor, statement, parameters, context, executemany):
        contadas.append(statement)

    event.listen(engine, "before_cursor_execute", _registrar)
    try:
        resultado = funcao()
    finally:
        event.remove(engine, "before_cursor_execute", _registrar)
    return len(contadas), resultado


@pytest.mark.parametrize("aprovacao", [True, False], ids=["aprovacao_ligada", "desligada"])
def test_lote_de_500_chaves_tem_numero_fixo_de_consultas(
    db_session, cenario, monkeypatch, aprovacao
):
    monkeypatch.setattr(settings, "aprovacao_justificativas_habilitada", aprovacao)
    c = cenario()
    quantidade = MAXIMO_LINHAS_POR_LOTE + 5
    banco = c.par(
        [(f"{indice + 1}.00", f"linha {indice}") for indice in range(quantidade)],
        [("0.50", "so sistema")],
    )
    chaves = [i["chave"] for i in c.listar(banco, limit=1000)["itens"] if i["lancamento_banco"]]
    assert len(chaves) == quantidade
    # A primeira chamada depois do commit recarrega o que ele expirou.
    assert c.justificar(banco, ["f" * 64]).status_code == 200

    pequeno, resposta_5 = _contar(lambda: c.justificar(banco, chaves[:5]))
    cheio, resposta_500 = _contar(lambda: c.justificar(banco, chaves[5:]))

    assert resposta_5.json()["processadas"] == 5
    assert resposta_500.json()["processadas"] == MAXIMO_LINHAS_POR_LOTE
    assert pequeno == cheio


def test_listagem_com_pagina_cheia_de_lotes_tem_numero_fixo_de_consultas(db_session, cenario):
    c = cenario()
    quantidade = 60
    banco = c.par(
        [(f"{indice + 1}.00", f"linha {indice}") for indice in range(quantidade)],
        [("0.50", "so sistema")],
    )
    chaves = [i["chave"] for i in c.listar(banco, limit=1000)["itens"]]
    # Vários lotes de tamanhos diferentes, um por fatia.
    inicio = 0
    for tamanho in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 6):
        resposta = c.justificar(banco, chaves[inicio : inicio + tamanho])
        assert resposta.json()["processadas"] == tamanho
        inicio += tamanho
    assert inicio == len(chaves)

    c.listar(banco, limit=1)  # recarrega o que o commit expirou
    pequena, lista_5 = _contar(lambda: c.listar(banco, limit=5))
    cheia, lista_50 = _contar(lambda: c.listar(banco, limit=50))

    assert len(lista_5["itens"]) == 5 and len(lista_50["itens"]) == 50
    assert all(i["decisao"]["lote_tamanho"] for i in lista_50["itens"])
    assert pequena == cheia


# --- onde os campos novos não aparecem --------------------------------------------------


def _alcancam(esquemas, nomes):
    """Os esquemas que chegam, direta ou indiretamente, a algum de `nomes`."""
    alcancam = set(nomes)
    while True:
        novos = {
            nome
            for nome, esquema in esquemas.items()
            if nome not in alcancam
            and any(f'/{outro}"' in json.dumps(esquema) for outro in alcancam)
        }
        if not novos:
            return alcancam
        alcancam |= novos


def test_campos_novos_so_nas_rotas_do_app_que_os_usam(db_session, cenario):
    openapi = app.openapi()
    esquemas = openapi["components"]["schemas"]

    def com_o_campo(campo):
        return {nome for nome, e in esquemas.items() if campo in e.get("properties", {})}

    assert com_o_campo("lote_tamanho") == {"DecisaoEmVigorResponse"}
    assert com_o_campo("lote_id") == {
        "DecisaoResponse",
        "DecisaoEmVigorResponse",
        "DecisaoLoteResponse",
    }
    alcancam = _alcancam(esquemas, com_o_campo("lote_id") | com_o_campo("lote_tamanho"))
    operacoes = {
        f"{metodo.upper()} {caminho}"
        for caminho, metodos in openapi["paths"].items()
        for metodo, operacao in metodos.items()
        if any(f'/{nome}"' in json.dumps(operacao) for nome in alcancam)
    }
    assert operacoes == {
        "GET /conciliacoes/{extrato_id}",
        "POST /conciliacoes/{extrato_id}/decisoes",
        "POST /conciliacoes/{extrato_id}/decisoes/lote",
    }
    # A /v1 tem OpenAPI próprio: nenhum dos dois campos.
    v1 = client.get("/v1/openapi.json")
    assert v1.status_code == 200
    assert "lote_id" not in v1.text and "lote_tamanho" not in v1.text

    # A exportação CSV não traz decisões, e os campos muito menos.
    c = cenario()
    banco = c.par()
    lote_id = c.justificar(banco, [c.chave(banco, "20.00")]).json()["lote_id"]
    csv = client.get(f"/conciliacoes/{banco.id}/exportar", headers=c.headers["admin"])
    assert csv.status_code == 200
    for proibido in ("lote", lote_id):
        assert proibido not in csv.text


def test_log_tem_ids_e_contagens_e_nunca_o_texto_nem_as_chaves(db_session, cenario, caplog):
    c = cenario()
    banco = c.par()
    chaves = [c.chave(banco, "20.00"), c.chave(banco, "99.00"), "f" * 64]

    with caplog.at_level(logging.INFO, logger="app.api.conciliacoes"):
        corpo = c.justificar(banco, chaves).json()

    (registro,) = [r for r in caplog.records if r.getMessage().startswith("decisoes_lote")]
    mensagem = registro.getMessage()
    assert f"extrato_banco_id={banco.id}" in mensagem
    assert f"lote_id={corpo['lote_id']}" in mensagem
    assert "processadas=2 ignoradas=1 nao_encontrada=1" in mensagem
    assert "resultado=gravado" in mensagem
    assert TEXTO not in caplog.text
    for chave in chaves:
        assert chave not in caplog.text
