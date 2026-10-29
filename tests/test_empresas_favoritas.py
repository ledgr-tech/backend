"""Favoritos de empresa por usuário e `GET /empresas` paginado (issue #157).

`PUT`/`DELETE /empresas/{id}/favorita` e a listagem com busca, ordem,
paginação, `favorita` e `status`. Regras em app/api/empresas.py.
"""

import uuid
from datetime import date

import pytest
from conftest import SENHA_USUARIO_TESTE, _gerar_cnpj, _gerar_cnpj_alfanumerico, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError

from app.core.cnpj import digito_verificador
from app.core.database import engine
from app.core.rate_limit import LIMITE_FAVORITAS_POR_USUARIO
from app.models import Empresa, EmpresaFavorita, ExecucaoConciliacao, Extrato, Fechamento
from main import app

client = TestClient(app)


@pytest.fixture
def conta(db_session, criar_usuario, gerar_token):
    """Admin e analista na mesma conta, com os cabeçalhos de cada um. A conta
    já nasce com uma empresa, a "Empresa de teste" de `criar_usuario`."""
    admin = criar_usuario(nome="Ana Admin")
    analista = criar_usuario(conta_id=admin.conta_id, papel="analista")
    headers = {
        "admin": cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id)),
        "analista": cabecalhos(gerar_token(analista.conta_id, usuario_id=analista.id)),
    }
    return admin.conta_id, {"admin": admin, "analista": analista}, headers


def _listar(headers, **params):
    return client.get("/empresas", headers=headers, params=params)


def _ids(resposta) -> list[str]:
    assert resposta.status_code == 200, resposta.text
    return [item["id"] for item in resposta.json()["itens"]]


def _favoritar(headers, empresa_id):
    return client.put(f"/empresas/{empresa_id}/favorita", headers=headers)


def _desfavoritar(headers, empresa_id):
    return client.delete(f"/empresas/{empresa_id}/favorita", headers=headers)


def _favoritas(db_session, usuario_id) -> set[uuid.UUID]:
    db_session.expire_all()
    return set(
        db_session.scalars(
            select(EmpresaFavorita.empresa_id).where(EmpresaFavorita.usuario_id == usuario_id)
        )
    )


def _conciliar(db_session, empresa: Empresa, periodo_inicio: date) -> None:
    """Um par conciliado com o extrato do banco começando em `periodo_inicio`."""
    banco, sistema = (
        Extrato(
            empresa_id=empresa.id,
            nome_arquivo=f"{origem}.ofx",
            formato="ofx",
            tamanho_bytes=10,
            origem=origem,
            status="concluido",
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_inicio,
        )
        for origem in ("banco", "sistema")
    )
    db_session.add_all([banco, sistema])
    db_session.flush()
    db_session.add(
        ExecucaoConciliacao(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            tolerancia_dias=0,
            total=0,
            match_exato=0,
            match_tolerancia=0,
            duplicado=0,
            sem_correspondencia=0,
            tarifa_bancaria=0,
            divergente_valor=0,
            divergente_data=0,
        )
    )
    db_session.commit()


def _fechar(db_session, empresa: Empresa, competencia: str, reaberto: bool = False) -> None:
    db_session.add(
        Fechamento(
            empresa_id=empresa.id,
            competencia=competencia,
            resumo={},
            fechado_por_nome="Ana Admin",
            reaberto_em=func.now() if reaberto else None,
            reaberto_por_nome="Ana Admin" if reaberto else None,
        )
    )
    db_session.commit()


# --- favoritar e desfavoritar -------------------------------------------------------


@pytest.mark.parametrize("papel", ["admin", "analista"])
def test_favoritar_e_desfavoritar_sao_idempotentes(db_session, conta, criar_empresa, papel):
    conta_id, usuarios, headers = conta
    empresa = criar_empresa("Beta", conta_id=conta_id)

    assert _favoritar(headers[papel], empresa.id).status_code == 204
    criado_em = db_session.scalar(
        select(EmpresaFavorita.criado_em).where(EmpresaFavorita.empresa_id == empresa.id)
    )
    assert _favoritar(headers[papel], empresa.id).status_code == 204
    assert _favoritas(db_session, usuarios[papel].id) == {empresa.id}
    # Favoritar de novo não regrava.
    assert (
        db_session.scalar(
            select(EmpresaFavorita.criado_em).where(EmpresaFavorita.empresa_id == empresa.id)
        )
        == criado_em
    )

    assert _desfavoritar(headers[papel], empresa.id).status_code == 204
    assert _desfavoritar(headers[papel], empresa.id).status_code == 204
    assert _favoritas(db_session, usuarios[papel].id) == set()


@pytest.mark.parametrize("acao", [_favoritar, _desfavoritar])
def test_empresa_de_outra_conta_ou_inexistente_e_404(db_session, conta, criar_empresa, acao):
    _, usuarios, headers = conta
    de_outra_conta = criar_empresa("Outra conta")

    for empresa_id in [de_outra_conta.id, uuid.uuid4()]:
        resposta = acao(headers["admin"], empresa_id)
        assert resposta.status_code == 404
        assert resposta.json() == {"detail": "Empresa não encontrada."}
    assert _favoritas(db_session, usuarios["admin"].id) == set()


def test_favoritar_sem_token_e_401(db_session, criar_empresa):
    empresa = criar_empresa()
    assert client.put(f"/empresas/{empresa.id}/favorita").status_code == 401
    assert client.delete(f"/empresas/{empresa.id}/favorita").status_code == 401


def test_favoritar_tem_limite_por_usuario(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    empresa = criar_empresa("Beta", conta_id=conta_id)
    limite = int(LIMITE_FAVORITAS_POR_USUARIO.split("/")[0])

    for _ in range(limite):
        assert _favoritar(headers["admin"], empresa.id).status_code == 204
    assert _favoritar(headers["admin"], empresa.id).status_code == 429
    # O limite é de cada usuário: o colega da mesma conta continua.
    assert _favoritar(headers["analista"], empresa.id).status_code == 204


def test_favorito_de_empresa_de_outra_conta_nao_existe_nem_direto_no_banco(
    db_session, conta, criar_empresa
):
    conta_id, usuarios, _ = conta
    de_outra_conta = criar_empresa("Outra conta")

    for conta_da_linha in [conta_id, de_outra_conta.conta_id]:
        db_session.add(
            EmpresaFavorita(
                usuario_id=usuarios["admin"].id,
                empresa_id=de_outra_conta.id,
                conta_id=conta_da_linha,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# --- listagem: favoritas, isolamento ------------------------------------------------


@pytest.mark.parametrize("ordem", ["nome", "cnpj", "status"])
@pytest.mark.parametrize("direcao", ["asc", "desc"])
def test_favoritas_vem_primeiro_em_qualquer_ordem(db_session, conta, criar_empresa, ordem, direcao):
    conta_id, _, headers = conta
    empresas = [criar_empresa(nome, conta_id=conta_id) for nome in ["Alfa", "Gama", "Zeta"]]
    _conciliar(db_session, empresas[1], date(2026, 9, 1))
    favoritas = {str(empresas[2].id), str(empresas[0].id)}
    for empresa_id in favoritas:
        _favoritar(headers["admin"], empresa_id)

    resposta = _listar(headers["admin"], ordem=ordem, direcao=direcao)

    itens = resposta.json()["itens"]
    assert {item["id"] for item in itens[:2]} == favoritas
    assert [item["favorita"] for item in itens] == [True, True, False, False]
    # Dentro de cada grupo vale a ordem pedida.
    chave = {"nome": "razao_social", "cnpj": "cnpj", "status": "status"}[ordem]
    for grupo in [itens[:2], itens[2:]]:
        valores = [item[chave] for item in grupo]
        assert valores == sorted(valores, reverse=direcao == "desc")


def test_ordem_padrao_e_nome_sem_diferenciar_acento_nem_maiuscula(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    for nome in ["beta", "Ágil", "Zeta", "Alfa"]:
        criar_empresa(nome, conta_id=conta_id)

    nomes = [item["razao_social"] for item in _listar(headers["admin"]).json()["itens"]]

    assert nomes == ["Ágil", "Alfa", "beta", "Empresa de teste", "Zeta"]


def test_favoritos_sao_do_usuario_e_nao_da_conta(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    empresa = criar_empresa("Zeta", conta_id=conta_id)
    _favoritar(headers["admin"], empresa.id)

    do_admin = _listar(headers["admin"]).json()["itens"]
    do_analista = _listar(headers["analista"]).json()["itens"]

    assert do_admin[0] == {**do_admin[0], "id": str(empresa.id), "favorita": True}
    assert not any(item["favorita"] for item in do_analista)
    assert do_analista[-1]["id"] == str(empresa.id)


def test_lista_so_as_empresas_da_conta(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    minha = criar_empresa("Minha", conta_id=conta_id)
    outra = criar_empresa("Minha", cnpj=minha.cnpj)

    ids = _ids(_listar(headers["admin"], busca="minha"))

    assert ids == [str(minha.id)]
    assert str(outra.id) not in _ids(_listar(headers["admin"], limit=100))


def test_item_traz_os_campos_de_antes_e_os_novos(db_session, conta):
    _, _, headers = conta

    corpo = _listar(headers["admin"]).json()

    assert set(corpo) == {"total", "limit", "offset", "itens"}
    assert set(corpo["itens"][0]) == {
        "id",
        "razao_social",
        "cnpj",
        "cnpj_verificado_em",
        "criado_em",
        "favorita",
        "status",
    }


# --- busca --------------------------------------------------------------------------


@pytest.mark.parametrize("busca", ["sao joao", "SÃO JOÃO", "joão", "padaria são", "Joao"])
def test_busca_por_nome_sem_diferenciar_acento_nem_maiuscula(
    db_session, conta, criar_empresa, busca
):
    conta_id, _, headers = conta
    padaria = criar_empresa("Padaria São João Ltda", conta_id=conta_id)
    criar_empresa("Mercado Central", conta_id=conta_id)

    assert _ids(_listar(headers["admin"], busca=busca)) == [str(padaria.id)]


def test_busca_sem_acento_acha_nome_gravado_com_e_vice_versa(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    acougue = criar_empresa("Açougue Irmãos", conta_id=conta_id)
    cafe = criar_empresa("Cafe Centro", conta_id=conta_id)

    assert _ids(_listar(headers["admin"], busca="acougue irmaos")) == [str(acougue.id)]
    assert _ids(_listar(headers["admin"], busca="café")) == [str(cafe.id)]


def test_busca_por_cnpj_com_e_sem_mascara(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    cnpj = _gerar_cnpj()
    empresa = criar_empresa("Cliente", conta_id=conta_id, cnpj=cnpj)
    mascarado = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"

    for busca in [
        cnpj,
        mascarado,
        cnpj[:8],
        f"{cnpj[:2]}.{cnpj[2:5]}",
        f"{cnpj[8:12]}-{cnpj[12:]}",
    ]:
        assert _ids(_listar(headers["admin"], busca=busca)) == [str(empresa.id)], busca


def test_busca_por_cnpj_alfanumerico_em_minusculas_e_com_mascara(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    cnpj = _gerar_cnpj_alfanumerico()
    empresa = criar_empresa("Cliente", conta_id=conta_id, cnpj=cnpj)
    mascarado = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}".lower()

    assert _ids(_listar(headers["admin"], busca=mascarado)) == [str(empresa.id)]


def test_busca_so_com_letras_nao_procura_no_cnpj(db_session, conta, criar_empresa):
    """Um CNPJ alfanumérico pode ter as letras da busca: sem dígito, a busca é
    só pelo nome."""
    conta_id, _, headers = conta
    base = "ABCDEFGH0001"
    base += digito_verificador(base)
    cnpj = base + digito_verificador(base)
    criar_empresa("Cliente", conta_id=conta_id, cnpj=cnpj)

    assert _ids(_listar(headers["admin"], busca="abcd")) == []


def test_curinga_do_like_na_busca_e_texto(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    criar_empresa("Cem Por Cento", conta_id=conta_id)
    com_porcento = criar_empresa("100% Digital", conta_id=conta_id)

    assert _ids(_listar(headers["admin"], busca="%")) == [str(com_porcento.id)]
    assert _ids(_listar(headers["admin"], busca="_")) == []


def test_busca_vazia_lista_todas(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    criar_empresa("Beta", conta_id=conta_id)

    assert _listar(headers["admin"], busca="   ").json()["total"] == 2


# --- paginação e validação ----------------------------------------------------------


def test_paginacao_de_20_com_total(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    for i in range(24):
        criar_empresa(f"Cliente {i:02d}", conta_id=conta_id)
    todas = _ids(_listar(headers["admin"], limit=100))
    assert len(todas) == 25

    primeira = _listar(headers["admin"]).json()
    segunda = _listar(headers["admin"], offset=20).json()

    assert (primeira["total"], primeira["limit"], primeira["offset"]) == (25, 20, 0)
    assert (segunda["total"], segunda["limit"], segunda["offset"]) == (25, 20, 20)
    assert [i["id"] for i in primeira["itens"] + segunda["itens"]] == todas
    assert len(segunda["itens"]) == 5
    # O total é o da busca, não o da conta.
    filtrada = _listar(headers["admin"], busca="cliente 1", limit=3).json()
    assert (filtrada["total"], len(filtrada["itens"])) == (10, 3)


def test_offset_alem_do_total_e_busca_sem_resultado_dao_lista_vazia(
    db_session, conta, criar_empresa
):
    conta_id, _, headers = conta
    criar_empresa("Cliente A", conta_id=conta_id)
    criar_empresa("Cliente B", conta_id=conta_id)
    criar_empresa("Outra Ltda", conta_id=conta_id)

    alem = _listar(headers["admin"], offset=40)
    filtrada = _listar(headers["admin"], busca="cliente", offset=2)

    assert alem.status_code == 200
    # O total é o da conta, mesmo sem itens na página.
    assert alem.json() == {"total": 4, "limit": 20, "offset": 40, "itens": []}
    assert filtrada.status_code == 200
    # E o total respeita a busca.
    assert filtrada.json() == {"total": 2, "limit": 20, "offset": 2, "itens": []}
    vazia = _listar(headers["admin"], busca="nenhuma empresa assim")
    assert vazia.status_code == 200
    assert vazia.json() == {"total": 0, "limit": 20, "offset": 0, "itens": []}


@pytest.mark.parametrize(
    "params",
    [
        {"ordem": "razao_social"},
        {"ordem": ""},
        {"direcao": "crescente"},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"busca": "x" * 101},
    ],
)
def test_parametro_invalido_e_422(db_session, conta, params):
    _, _, headers = conta
    assert _listar(headers["admin"], **params).status_code == 422


# --- status -------------------------------------------------------------------------


def test_status_nos_tres_casos(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    fechada = criar_empresa("Fechada", conta_id=conta_id)
    _conciliar(db_session, fechada, date(2026, 9, 3))
    _fechar(db_session, fechada, "2026-09")
    aberta = criar_empresa("Aberta", conta_id=conta_id)
    _conciliar(db_session, aberta, date(2026, 9, 3))
    reaberta = criar_empresa("Reaberta", conta_id=conta_id)
    _conciliar(db_session, reaberta, date(2026, 9, 3))
    _fechar(db_session, reaberta, "2026-09", reaberto=True)
    nunca = criar_empresa("Nunca", conta_id=conta_id)

    status = {i["id"]: i["status"] for i in _listar(headers["admin"]).json()["itens"]}

    assert status[str(fechada.id)] == "fechado"
    assert status[str(aberta.id)] == "em_aberto"
    assert status[str(reaberta.id)] == "em_aberto"
    assert status[str(nunca.id)] == "sem_conciliacao"


def test_status_e_o_do_mes_mais_recente_com_conciliacao(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    antigo_fechado = criar_empresa("Antigo fechado", conta_id=conta_id)
    _conciliar(db_session, antigo_fechado, date(2026, 8, 1))
    _fechar(db_session, antigo_fechado, "2026-08")
    _conciliar(db_session, antigo_fechado, date(2026, 9, 1))
    recente_fechado = criar_empresa("Recente fechado", conta_id=conta_id)
    _conciliar(db_session, recente_fechado, date(2026, 8, 1))
    _conciliar(db_session, recente_fechado, date(2026, 9, 30))
    _fechar(db_session, recente_fechado, "2026-09")
    # Extrato do banco sem conciliação não define o mês.
    db_session.add(
        Extrato(
            empresa_id=recente_fechado.id,
            nome_arquivo="banco.ofx",
            formato="ofx",
            tamanho_bytes=10,
            origem="banco",
            status="concluido",
            periodo_inicio=date(2026, 10, 1),
            periodo_fim=date(2026, 10, 31),
        )
    )
    db_session.commit()

    status = {i["id"]: i["status"] for i in _listar(headers["admin"]).json()["itens"]}

    assert status[str(antigo_fechado.id)] == "em_aberto"
    assert status[str(recente_fechado.id)] == "fechado"


def test_ordem_por_status(db_session, conta, criar_empresa):
    conta_id, _, headers = conta
    fechada = criar_empresa("Fechada", conta_id=conta_id)
    _conciliar(db_session, fechada, date(2026, 9, 3))
    _fechar(db_session, fechada, "2026-09")
    aberta = criar_empresa("Aberta", conta_id=conta_id)
    _conciliar(db_session, aberta, date(2026, 9, 3))
    primeira = _listar(headers["admin"], busca="empresa de teste").json()["itens"][0]["id"]

    asc = _ids(_listar(headers["admin"], ordem="status"))
    desc = _ids(_listar(headers["admin"], ordem="status", direcao="desc"))

    assert asc == [str(aberta.id), str(fechada.id), primeira]
    assert desc == [primeira, str(fechada.id), str(aberta.id)]


# --- uma consulta por página --------------------------------------------------------


def _selects(funcao) -> int:
    selects: list[str] = []

    def contar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", contar)
    try:
        resposta = funcao()
    finally:
        event.remove(engine, "before_cursor_execute", contar)
    assert resposta.status_code == 200, resposta.text
    return len(selects)


def test_numero_de_consultas_nao_depende_das_empresas(db_session, conta, criar_empresa):
    conta_id, _, headers = conta

    def chamar():
        return _listar(headers["admin"], ordem="status", busca="e")

    pequeno = _selects(chamar)
    for i in range(6):
        empresa = criar_empresa(f"Empresa {i}", conta_id=conta_id)
        _conciliar(db_session, empresa, date(2026, 9, 1 + i))
        if i % 2:
            _fechar(db_session, empresa, "2026-09")
            _favoritar(headers["admin"], empresa.id)

    assert _selects(chamar) == pequeno


# --- favoritos somem com o usuário e com a empresa ----------------------------------


def test_desativar_a_conta_apaga_os_favoritos_do_usuario(db_session, conta, criar_empresa):
    conta_id, usuarios, headers = conta
    empresa = criar_empresa("Beta", conta_id=conta_id)
    _favoritar(headers["admin"], empresa.id)
    _favoritar(headers["analista"], empresa.id)

    resposta = client.request(
        "DELETE", "/me", json={"senha_atual": SENHA_USUARIO_TESTE}, headers=headers["analista"]
    )

    assert resposta.status_code == 204, resposta.text
    assert _favoritas(db_session, usuarios["analista"].id) == set()
    assert _favoritas(db_session, usuarios["admin"].id) == {empresa.id}


def test_apagar_a_empresa_ou_o_usuario_apaga_os_favoritos(
    db_session, conta, criar_empresa, criar_usuario, gerar_token
):
    conta_id, usuarios, headers = conta
    empresa = criar_empresa("Beta", conta_id=conta_id)
    outra = criar_empresa("Gama", conta_id=conta_id)
    temporario = criar_usuario(conta_id=conta_id, papel="analista")
    cabecalho = cabecalhos(gerar_token(conta_id, usuario_id=temporario.id))
    _favoritar(headers["admin"], empresa.id)
    _favoritar(headers["admin"], outra.id)
    _favoritar(cabecalho, outra.id)

    db_session.execute(delete(Empresa).where(Empresa.id == empresa.id))
    db_session.execute(delete(type(temporario)).where(type(temporario).id == temporario.id))
    db_session.commit()

    assert _favoritas(db_session, usuarios["admin"].id) == {outra.id}
    assert _favoritas(db_session, temporario.id) == set()
