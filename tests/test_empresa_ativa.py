"""Empresa ativa e acesso cruzado (issue #133, ADR-019).

A conta A tem duas empresas clientes, A1 e A2, com ids diferentes do id da
conta (para nenhum bug que confunda `conta_id` com `empresa_id` passar
escondido); a conta B tem uma, B1. Para cada rota por empresa:

- sem `X-Ledgr-Empresa`, na conta A: 400 "Escolha a empresa.";
- com a empresa da conta B: 404 "Empresa não encontrada.", a mesma mensagem
  do uuid inexistente;
- valor que não é uuid: 400;
- recurso da A1 pedido com a A2 ativa: 404 (ou, nas rotas sem id, nada da A1
  aparece);
- recurso certo com a empresa certa: 200 (201 onde a rota cria).

E ainda: a conta de uma empresa só continua funcionando sem o cabeçalho (o
front de antes da #133), as rotas da conta ignoram o cabeçalho, o 402 do
`POST /conciliacoes` olha a situação da conta, e `exigir_papel` dá 403 com
quem pode.
"""

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Annotated

import pytest
from conftest import cabecalhos
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_explicacoes import ProvedorFalso

from app.api.explicacoes import obter_provedor_dependencia
from app.core import auth
from app.core.auth import CABECALHO_EMPRESA, exigir_papel
from app.core.config import settings
from app.models import Conciliacao, Configuracao, Conta, Extrato, Usuario
from main import app

client = TestClient(app)

SETEMBRO = (date(2026, 9, 1), date(2026, 9, 30))
ESCOLHA = {"detail": "Escolha a empresa."}
NAO_ENCONTRADA = {"detail": "Empresa não encontrada."}
CSV_VAZIO = b"data,valor,descricao\n"


@pytest.fixture
def contas(
    db_session,
    criar_conta,
    criar_empresa,
    criar_usuario,
    criar_extrato_da_empresa,
    inserir_lancamentos,
    gerar_token,
):
    conta_a = criar_conta("Escritório A")
    a1 = criar_empresa("Cliente A1", conta_id=conta_a.id)
    a2 = criar_empresa("Cliente A2", conta_id=conta_a.id)
    b1 = criar_empresa("Empresa B")
    assert len({conta_a.id, a1.id, a2.id, b1.conta_id, b1.id}) == 5
    admin_a = criar_usuario(conta_id=conta_a.id, papel="admin")
    admin_b = criar_usuario(conta_id=b1.conta_id, papel="admin")
    token_a = gerar_token(conta_a.id, usuario_id=admin_a.id)
    token_b = gerar_token(b1.conta_id, usuario_id=admin_b.id)

    def extrato(origem, itens):
        novo = criar_extrato_da_empresa(a1.id, origem)
        inserir_lancamentos(novo, itens)
        novo.periodo_inicio, novo.periodo_fim = SETEMBRO
        db_session.commit()
        return novo

    # Dado da A1: um par conciliado pela API, com uma linha divergente.
    banco = extrato("banco", [("10.00", "a"), ("20.00", "b")])
    sistema = extrato("sistema", [("10.00", "a")])
    resposta = client.post(
        "/conciliacoes",
        headers=cabecalhos(token_a, a1.id),
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert resposta.status_code == 201, resposta.text
    divergente = db_session.scalars(
        select(Conciliacao).where(
            Conciliacao.extrato_banco_id == banco.id,
            Conciliacao.status == "sem_correspondencia",
        )
    ).one()
    itens = client.get(f"/conciliacoes/{banco.id}", headers=cabecalhos(token_a, a1.id)).json()
    chave = next(
        item["chave"]
        for item in itens["itens"]
        if item["lancamento_banco"] and item["lancamento_banco"]["valor"] == "20.00"
    )

    return SimpleNamespace(
        conta_a=conta_a,
        a1=a1,
        a2=a2,
        b1=b1,
        admin_a=admin_a,
        token_a=token_a,
        token_b=token_b,
        banco=banco,
        sistema=sistema,
        divergente=divergente,
        chave=chave,
    )


def _a(contas, empresa=None, **extra) -> dict[str, str]:
    """Cabeçalhos do admin da conta A, com `empresa` ativa (ou sem o cabeçalho)."""
    return {**cabecalhos(contas.token_a, empresa.id if empresa else None), **extra}


def _pedir(rota, contas, headers):
    metodo, caminho, kwargs = rota(contas)
    return client.request(metodo, caminho, headers=headers, **kwargs)


def _par(c):
    return {"extrato_banco_id": str(c.banco.id), "extrato_sistema_id": str(c.sistema.id)}


# Toda rota por empresa: (método, caminho, kwargs) a partir do cenário.
ROTAS_POR_EMPRESA = {
    "listar_extratos": lambda c: ("GET", "/extratos", {}),
    "obter_extrato": lambda c: ("GET", f"/extratos/{c.banco.id}", {}),
    "upload": lambda c: (
        "POST",
        "/extratos/upload",
        {
            "files": {"arquivo": ("extrato.csv", CSV_VAZIO, "text/csv")},
            "data": {"origem": "banco"},
        },
    ),
    "conciliar": lambda c: ("POST", "/conciliacoes", {"json": _par(c)}),
    "listar_conciliacao": lambda c: ("GET", f"/conciliacoes/{c.banco.id}", {}),
    "exportar": lambda c: ("GET", f"/conciliacoes/{c.banco.id}/exportar", {}),
    "decidir": lambda c: (
        "POST",
        f"/conciliacoes/{c.banco.id}/decisoes",
        {"json": {"chave": c.chave, "tipo": "justificada", "texto": "motivo"}},
    ),
    "execucoes": lambda c: ("GET", "/execucoes", {"params": {"extrato_banco_id": str(c.banco.id)}}),
    "explicar": lambda c: (
        "POST",
        "/explicacoes",
        {"json": {"conciliacao_id": str(c.divergente.id)}},
    ),
    "fechar": lambda c: ("POST", "/fechamentos", {"json": {"competencia": "2026-09"}}),
    "listar_fechamentos": lambda c: ("GET", "/fechamentos", {}),
    "reabrir": lambda c: ("DELETE", "/fechamentos/2026-09", {}),
    "ler_configuracoes": lambda c: ("GET", "/empresa/configuracoes", {}),
    "alterar_configuracoes": lambda c: (
        "PUT",
        "/empresa/configuracoes",
        {"json": {"tolerancia_dias": 3}},
    ),
}


@pytest.fixture
def provedor_falso():
    falso = ProvedorFalso()
    app.dependency_overrides[obter_provedor_dependencia] = lambda: falso
    yield falso
    app.dependency_overrides.pop(obter_provedor_dependencia, None)


# --- as regras do cabeçalho, em toda rota por empresa ---------------------------


@pytest.mark.parametrize("rota", ROTAS_POR_EMPRESA.values(), ids=ROTAS_POR_EMPRESA.keys())
def test_sem_cabecalho_na_conta_com_duas_empresas_da_400(db_session, contas, rota):
    resposta = _pedir(rota, contas, _a(contas))

    assert resposta.status_code == 400
    assert resposta.json() == ESCOLHA


@pytest.mark.parametrize("rota", ROTAS_POR_EMPRESA.values(), ids=ROTAS_POR_EMPRESA.keys())
def test_empresa_de_outra_conta_da_404_como_inexistente(db_session, contas, rota):
    de_outra_conta = _pedir(rota, contas, _a(contas, contas.b1))
    inexistente = _pedir(rota, contas, _a(contas, **{CABECALHO_EMPRESA: str(uuid.uuid4())}))

    assert de_outra_conta.status_code == inexistente.status_code == 404
    assert de_outra_conta.json() == inexistente.json() == NAO_ENCONTRADA


@pytest.mark.parametrize("rota", ROTAS_POR_EMPRESA.values(), ids=ROTAS_POR_EMPRESA.keys())
def test_o_id_da_conta_no_cabecalho_nao_e_empresa(db_session, contas, rota):
    resposta = _pedir(rota, contas, _a(contas, **{CABECALHO_EMPRESA: str(contas.conta_a.id)}))

    assert resposta.status_code == 404
    assert resposta.json() == NAO_ENCONTRADA


@pytest.mark.parametrize("valor", ["nao-e-uuid", "", "123"])
@pytest.mark.parametrize(
    "rota",
    [ROTAS_POR_EMPRESA["listar_extratos"], ROTAS_POR_EMPRESA["alterar_configuracoes"]],
    ids=["get", "put"],
)
def test_cabecalho_que_nao_e_uuid_da_400(db_session, contas, rota, valor):
    resposta = _pedir(rota, contas, _a(contas, **{CABECALHO_EMPRESA: valor}))

    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "X-Ledgr-Empresa não é um id de empresa."}


def test_sem_token_continua_401_antes_do_cabecalho(db_session, contas):
    resposta = client.get("/extratos", headers={CABECALHO_EMPRESA: "nao-e-uuid"})

    assert resposta.status_code == 401


# --- recurso da A1 com a A2 ativa: 404; com a A1: 200 -----------------------------

ROTAS_COM_RECURSO = [
    "obter_extrato",
    "listar_conciliacao",
    "exportar",
    "decidir",
    "conciliar",
    "execucoes",
    "explicar",
]


@pytest.mark.parametrize("nome", ROTAS_COM_RECURSO)
def test_recurso_da_a1_com_a_a2_ativa_e_404_e_com_a_a1_e_200(
    db_session, contas, provedor_falso, nome
):
    rota = ROTAS_POR_EMPRESA[nome]

    errada = _pedir(rota, contas, _a(contas, contas.a2))
    certa = _pedir(rota, contas, _a(contas, contas.a1))

    assert errada.status_code == 404, errada.text
    assert errada.json() != NAO_ENCONTRADA  # é o recurso, não a empresa
    assert certa.status_code in (200, 201), certa.text


def test_listagem_de_extratos_so_mostra_a_empresa_ativa(db_session, contas):
    da_a1 = client.get("/extratos", headers=_a(contas, contas.a1)).json()["itens"]
    da_a2 = client.get("/extratos", headers=_a(contas, contas.a2)).json()["itens"]

    assert {item["extrato_id"] for item in da_a1} == {str(contas.banco.id), str(contas.sistema.id)}
    assert da_a2 == []


def test_upload_grava_na_empresa_ativa(db_session, contas):
    resposta = client.post(
        "/extratos/upload",
        headers=_a(contas, contas.a2),
        files={"arquivo": ("extrato.csv", CSV_VAZIO, "text/csv")},
        data={"origem": "banco"},
    )

    assert resposta.status_code in (201, 202), resposta.text
    extrato = db_session.get(Extrato, uuid.UUID(resposta.json()["extrato_id"]))
    assert extrato.empresa_id == contas.a2.id


def test_fechamento_e_da_empresa_ativa(db_session, contas):
    fechar = {"competencia": "2026-09", "ressalva": "pendência conhecida"}

    na_a2 = client.post("/fechamentos", headers=_a(contas, contas.a2), json=fechar)
    na_a1 = client.post("/fechamentos", headers=_a(contas, contas.a1), json=fechar)

    assert na_a2.status_code == 422
    assert na_a2.json() == {"detail": "Não há conciliação nesta competência."}
    assert na_a1.status_code == 201
    listar = ROTAS_POR_EMPRESA["listar_fechamentos"]
    assert _pedir(listar, contas, _a(contas, contas.a2)).json()["itens"] == []
    assert len(_pedir(listar, contas, _a(contas, contas.a1)).json()["itens"]) == 1

    reabrir = ROTAS_POR_EMPRESA["reabrir"]
    assert _pedir(reabrir, contas, _a(contas, contas.a2)).status_code == 404
    assert _pedir(reabrir, contas, _a(contas, contas.a1)).status_code == 200


def test_configuracao_continua_por_empresa(db_session, contas):
    alterar = ROTAS_POR_EMPRESA["alterar_configuracoes"]
    ler = ROTAS_POR_EMPRESA["ler_configuracoes"]

    assert _pedir(alterar, contas, _a(contas, contas.a1)).status_code == 200

    assert _pedir(ler, contas, _a(contas, contas.a1)).json()["tolerancia_dias"] == 3
    assert _pedir(ler, contas, _a(contas, contas.a2)).json()["tolerancia_dias"] == 0
    db_session.expire_all()
    assert (
        db_session.scalar(
            select(Configuracao.tolerancia_dias_default).where(
                Configuracao.empresa_id == contas.a1.id
            )
        )
        == 3
    )


def test_decisao_na_a1_nao_aparece_na_a2(db_session, contas):
    decidir = ROTAS_POR_EMPRESA["decidir"]
    assert _pedir(decidir, contas, _a(contas, contas.a1)).status_code == 200

    assert _pedir(decidir, contas, _a(contas, contas.a2)).status_code == 404


# --- conta de uma empresa só e rotas da conta -------------------------------------


@pytest.mark.parametrize("rota", ROTAS_POR_EMPRESA.values(), ids=ROTAS_POR_EMPRESA.keys())
def test_conta_de_uma_empresa_so_dispensa_o_cabecalho(db_session, contas, provedor_falso, rota):
    """O front de antes da #133 não manda o cabeçalho: com uma empresa só,
    vale essa, e a A1 da outra conta continua 404 (o recurso, não a empresa)."""
    resposta = _pedir(rota, contas, cabecalhos(contas.token_b))

    assert resposta.status_code != 400, resposta.text
    assert resposta.json() != NAO_ENCONTRADA if resposta.status_code == 404 else True


def test_conta_de_uma_empresa_so_sem_cabecalho_ve_os_proprios_extratos(
    db_session, contas, criar_extrato_da_empresa
):
    do_b = criar_extrato_da_empresa(contas.b1.id, "banco")

    itens = client.get("/extratos", headers=cabecalhos(contas.token_b)).json()["itens"]

    assert [item["extrato_id"] for item in itens] == [str(do_b.id)]


@pytest.mark.parametrize(
    ("metodo", "caminho", "kwargs"),
    [
        ("GET", "/me", {}),
        ("GET", "/empresa/usuarios", {}),
        ("GET", "/empresa/convites", {}),
        ("POST", "/empresa/plano", {"json": {"plano": "padrao"}}),
    ],
)
@pytest.mark.parametrize("valor", ["nao-e-uuid", None, "outra_conta"])
def test_rotas_da_conta_ignoram_o_cabecalho(db_session, contas, metodo, caminho, kwargs, valor):
    headers = _a(contas)
    if valor == "outra_conta":
        headers[CABECALHO_EMPRESA] = str(contas.b1.id)
    elif valor is not None:
        headers[CABECALHO_EMPRESA] = valor

    resposta = client.request(metodo, caminho, headers=headers, **kwargs)

    assert resposta.status_code == 200, resposta.text


def test_plano_e_um_so_para_as_empresas_da_conta(db_session, contas, monkeypatch):
    """O 402 do `POST /conciliacoes` olha a situação da conta (issue #133)."""
    monkeypatch.setattr(settings, "plano_bloqueio_habilitado", True)
    conta = db_session.get(Conta, contas.conta_a.id)
    conta.plano = "padrao"
    conta.teste_termina_em = auth._agora() - timedelta(days=1)
    db_session.commit()

    def conciliar_na_a2():
        banco = Extrato(
            empresa_id=contas.a2.id,
            nome_arquivo="b.csv",
            formato="csv",
            tamanho_bytes=0,
            origem="banco",
            status="concluido",
        )
        sistema = Extrato(
            empresa_id=contas.a2.id,
            nome_arquivo="s.csv",
            formato="csv",
            tamanho_bytes=0,
            origem="sistema",
            status="concluido",
        )
        db_session.add_all([banco, sistema])
        db_session.commit()
        return client.post(
            "/conciliacoes",
            headers=_a(contas, contas.a2),
            json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
        )

    bloqueada = conciliar_na_a2()
    assert bloqueada.status_code == 402
    assert client.get("/me", headers=_a(contas)).json()["situacao"] == "teste_encerrado"

    conta.assinatura_ativa = True
    db_session.commit()
    assert conciliar_na_a2().status_code == 201


# --- exigir_papel ------------------------------------------------------------------

app_papeis = FastAPI()


@app_papeis.get("/aprovar")
def _aprovar(usuario: Annotated[Usuario, Depends(exigir_papel("admin", "gestor"))]):
    return {"papel": usuario.papel}


@app_papeis.get("/plano")
def _plano(usuario: Annotated[Usuario, Depends(exigir_papel("admin"))]):
    return {"papel": usuario.papel}


cliente_papeis = TestClient(app_papeis)


@pytest.mark.parametrize(
    ("papel", "aprovar", "plano"),
    [("admin", 200, 200), ("gestor", 200, 403), ("analista", 403, 403)],
)
def test_exigir_papel(db_session, criar_usuario, gerar_token, papel, aprovar, plano):
    admin = criar_usuario()
    usuario = admin if papel == "admin" else criar_usuario(conta_id=admin.conta_id, papel=papel)
    headers = cabecalhos(gerar_token(usuario.conta_id, usuario_id=usuario.id))

    resposta_aprovar = cliente_papeis.get("/aprovar", headers=headers)
    resposta_plano = cliente_papeis.get("/plano", headers=headers)

    assert (resposta_aprovar.status_code, resposta_plano.status_code) == (aprovar, plano)
    if aprovar == 403:
        assert resposta_aprovar.json() == {
            "detail": "Só o admin ou o gestor da conta podem fazer isso."
        }
    if plano == 403:
        assert resposta_plano.json() == {"detail": "Só o admin da conta pode fazer isso."}


def test_exigir_papel_sem_papel_e_erro_de_programacao():
    with pytest.raises(ValueError):
        exigir_papel()


def test_exigir_papel_sem_token_e_401():
    assert cliente_papeis.get("/aprovar").status_code == 401
