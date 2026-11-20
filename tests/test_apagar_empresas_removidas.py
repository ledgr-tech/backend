# ruff: noqa: F811
"""Apagar de vez as empresas removidas há mais de 30 dias (issue #215, ADR-019):
scripts/apagar_empresas_removidas.py e a função de app/services/exclusao.py que
ele divide com scripts/excluir_empresa.py.

O relógio é injetado (`agora`), e o R2 é o `ArmazenamentoEmMemoria` do conftest:
nada acessa a internet. Os dados vêm das fábricas de tests/test_excluir_empresa.py.
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import ArmazenamentoEmMemoria, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_excluir_empresa import (  # noqa: F401 - fixtures e helpers
    NOME_ADMIN,
    _existentes,
    conta_povoada,
    registros,
)

from app.models import Base, Empresa, Extrato, LinhaInvalida, RegistroExclusao
from app.services import exclusao as servico
from app.services.arquivo_original import chave_do_objeto, prefixo_da_empresa
from app.services.exclusao import ETAPAS, Escopo
from main import app
from scripts import apagar_empresas_removidas as script
from scripts import excluir_empresa

client = TestClient(app)

AGORA = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def _remover(db_session, empresa_id, *, apagar_em=AGORA - timedelta(days=1)):
    """Marca a empresa como removida, direto no banco, com o prazo dado."""
    db_session.execute(
        update(Empresa)
        .where(Empresa.id == empresa_id)
        .values(
            removida_em=apagar_em - timedelta(days=30),
            removida_por_nome="Quem Removeu",
            apagar_em=apagar_em,
        ),
        execution_options={"synchronize_session": False},
    )
    db_session.commit()
    db_session.expire_all()


def _rodar(*argumentos, armazenamento=None, agora=AGORA) -> int:
    return script.main(
        list(argumentos),
        agora=lambda: agora,
        criar_armazenamento=lambda: armazenamento,
    )


def _contagens(db_session, conta_id, empresa_id=None) -> dict[str, int]:
    db_session.rollback()
    return servico.contar_linhas(db_session, Escopo(conta_id, empresa_id))


def _empresas(db_session, conta_id) -> list[SimpleNamespace]:
    """As empresas da conta, removidas ou não, como valores soltos: o objeto da
    sessão não sobrevive à exclusão da linha dele."""
    db_session.expire_all()
    return [
        SimpleNamespace(id=e.id, conta_id=e.conta_id, cnpj=e.cnpj, razao_social=e.razao_social)
        for e in db_session.scalars(
            select(Empresa)
            .where(Empresa.conta_id == conta_id)
            .order_by(Empresa.criado_em, Empresa.id)
            .execution_options(incluir_removidas=True)
        )
    ]


def _ids_dos_registros(db_session, *empresa_ids) -> list[RegistroExclusao]:
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(RegistroExclusao).where(RegistroExclusao.empresa_id.in_(empresa_ids))
        )
    )


@pytest.fixture
def limpar_registros(db_session):
    ids: list[uuid.UUID] = []
    yield ids.append
    db_session.rollback()
    db_session.query(RegistroExclusao).filter(RegistroExclusao.empresa_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.commit()


# --- o que sai ------------------------------------------------------------------------


def test_apaga_a_empresa_vencida_e_o_que_e_dela_e_nada_do_resto(
    db_session, conta_povoada, limpar_registros
):
    conta, _ = conta_povoada()
    vencida, ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    _remover(db_session, vencida.id)
    antes_da_conta = _contagens(db_session, conta.id)
    antes_da_vencida = _contagens(db_session, conta.id, vencida.id)
    antes_da_ativa = _contagens(db_session, conta.id, ativa.id)
    assert antes_da_vencida["empresas"] == 1 and antes_da_vencida["extratos"] == 2

    codigo = _rodar()

    assert codigo == 0
    assert _contagens(db_session, conta.id, vencida.id) == dict.fromkeys(antes_da_vencida, 0)
    assert _contagens(db_session, conta.id, ativa.id) == antes_da_ativa
    # O que é da conta (usuários, convites, plano, a conta) não saiu: a conta
    # só perdeu as linhas da empresa.
    depois_da_conta = _contagens(db_session, conta.id)
    for tabela, quantidade in antes_da_conta.items():
        assert depois_da_conta[tabela] == quantidade - antes_da_vencida[tabela], tabela
    assert depois_da_conta["contas"] == 1 and depois_da_conta["usuarios"] == 2
    assert [e.id for e in _empresas(db_session, conta.id)] == [ativa.id]


def test_nao_toca_empresa_ativa_removida_no_prazo_nem_a_de_outra_conta(
    db_session, conta_povoada, limpar_registros
):
    conta, _ = conta_povoada()
    outra, linhas_da_outra = conta_povoada("Outra Conta Ltda")
    vencida, no_prazo = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    _remover(db_session, vencida.id)
    _remover(db_session, no_prazo.id, apagar_em=AGORA + timedelta(days=1))
    antes_do_prazo = _contagens(db_session, conta.id, no_prazo.id)
    antes_da_outra = _contagens(db_session, outra.id)

    assert _rodar() == 0

    assert [e.id for e in _empresas(db_session, conta.id)] == [no_prazo.id]
    assert _contagens(db_session, conta.id, no_prazo.id) == antes_do_prazo
    assert _contagens(db_session, outra.id) == antes_da_outra
    assert len(_existentes(db_session, linhas_da_outra)) == len(linhas_da_outra)


def test_o_relogio_decide_o_prazo(db_session, conta_povoada, limpar_registros):
    conta, _ = conta_povoada()
    removida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(removida.id)
    _remover(db_session, removida.id, apagar_em=AGORA + timedelta(days=5))

    assert _rodar(agora=AGORA + timedelta(days=5) - timedelta(seconds=1)) == 0
    assert len(_empresas(db_session, conta.id)) == 2
    assert _rodar(agora=AGORA + timedelta(days=5)) == 0
    assert len(_empresas(db_session, conta.id)) == 1


def test_e_idempotente(db_session, conta_povoada, limpar_registros, caplog):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    _remover(db_session, vencida.id)

    assert _rodar() == 0
    depois_da_primeira = _contagens(db_session, conta.id)
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert _rodar() == 0

    assert _contagens(db_session, conta.id) == depois_da_primeira
    assert len(_ids_dos_registros(db_session, vencida.id)) == 1
    assert "vencidas=0" in caplog.text


def test_sem_empresa_vencida_nao_faz_nada(db_session, conta_povoada):
    _, linhas = conta_povoada()

    assert _rodar() == 0

    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_simular_lista_e_nao_apaga(db_session, conta_povoada, caplog):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    _remover(db_session, vencida.id)
    antes = _contagens(db_session, conta.id)
    caplog.set_level(logging.INFO)

    assert _rodar("--simular") == 0

    assert _contagens(db_session, conta.id) == antes
    assert f"empresa_id={vencida.id}" in caplog.text and "simulação" in caplog.text


def test_empresa_ativa_nunca_e_apagada_pela_funcao_do_servico(db_session, conta_povoada):
    conta, _ = conta_povoada()
    ativa, _ = _empresas(db_session, conta.id)

    with pytest.raises(ValueError, match="não está removida"):
        servico.apagar_empresa(db_session, db_session.get(Empresa, ativa.id), "teste")

    db_session.rollback()
    assert _contagens(db_session, conta.id, ativa.id)["extratos"] == 2


# --- o mesmo que o excluir_empresa.py --------------------------------------------------


def test_a_empresa_leva_o_mesmo_que_a_exclusao_da_conta_leva_dela(db_session, conta_povoada):
    """Nas tabelas que a empresa leva, as linhas das duas empresas somam o que a
    exclusão da conta apaga. As que ficam são as da conta."""
    conta, _ = conta_povoada()
    primeira, segunda = _empresas(db_session, conta.id)

    da_conta = _contagens(db_session, conta.id)
    da_primeira = _contagens(db_session, conta.id, primeira.id)
    da_segunda = _contagens(db_session, conta.id, segunda.id)

    assert servico.tabelas_apagadas() == excluir_empresa.tabelas_apagadas()
    soma = {t: da_primeira[t] + da_segunda[t] for t in da_conta}
    levadas = {t for t, n in soma.items() if n}
    so_da_conta = {t for t in da_conta if t not in levadas}
    assert so_da_conta == {
        "convites",
        "pedidos_autorizacao_oauth",
        "tokens_email",
        "cupons_enviados",
        "eventos_conta",
        "pagamentos_stripe",
        "assinaturas_stripe",
        "operadores_ledgr",
        "usuarios",
        "usos_assistente",
        "contas",
    }
    for tabela in levadas:
        assert soma[tabela] == da_conta[tabela], tabela
    # Nenhuma linha da empresa conta duas vezes.
    assert da_primeira["empresas"] == da_segunda["empresas"] == 1


def test_toda_tabela_por_empresa_sai_no_escopo_da_empresa():
    """Uma tabela nova com `empresa_id` entra em `ETAPAS` (o teste da exclusão da
    conta cobra isso) e precisa sair também com a empresa: aqui falha se ela só
    sair com a conta inteira."""
    escopo = Escopo(uuid.uuid4(), uuid.uuid4())
    por_conexao = {"tokens_oauth", "codigos_oauth"}
    for model, condicao in ETAPAS:
        tabela = model.__tablename__
        por_empresa = "empresa_id" in model.__table__.c or tabela in {
            Empresa.__tablename__,
            LinhaInvalida.__tablename__,
            *por_conexao,
        }
        assert (condicao(escopo) is not None) == por_empresa, tabela


def test_a_ordem_das_etapas_respeita_as_fks_inclusive_a_de_quem_removeu():
    ordem = {t: i for i, t in enumerate(servico.tabelas_apagadas())}
    for tabela, posicao in ordem.items():
        for fk in Base.metadata.tables[tabela].foreign_keys:
            referenciada = fk.column.table.name
            if referenciada != tabela and referenciada in ordem:
                assert posicao < ordem[referenciada], f"{tabela} sai depois de {referenciada}"
    assert ordem["empresas"] < ordem["usuarios"]


# --- registro --------------------------------------------------------------------------


def test_grava_o_registro_da_exclusao_sem_nada_dos_dados(
    db_session, conta_povoada, limpar_registros
):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    cnpj, razao = vencida.cnpj, vencida.razao_social
    _remover(db_session, vencida.id)

    _rodar()

    (registro,) = _ids_dos_registros(db_session, vencida.id)
    assert registro.cnpj_sha256 == servico.hash_cnpj(cnpj)
    assert registro.executado_por == script.EXECUTADO_POR
    assert registro.contagens["empresas"] == 1 and registro.contagens["contas"] == 0
    assert set(servico.tabelas_apagadas()) <= set(registro.contagens)
    texto = json.dumps(registro.contagens) + registro.cnpj_sha256
    for proibido in (cnpj, razao, NOME_ADMIN, "@teste.com"):
        assert proibido not in texto


def test_o_log_so_tem_ids_e_contagens(db_session, conta_povoada, limpar_registros, caplog):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    cnpj, razao = vencida.cnpj, vencida.razao_social
    _remover(db_session, vencida.id)
    caplog.set_level(logging.INFO)

    _rodar()

    texto = caplog.text
    assert f"empresa_id={vencida.id}" in texto and "resultado=apagada" in texto
    assert "apagadas=1 puladas=0 falhas=0" in texto
    for proibido in (cnpj, razao, NOME_ADMIN, "Quem Removeu", "@teste.com"):
        assert proibido not in texto


# --- R2 --------------------------------------------------------------------------------


def _com_original(db_session, empresa, memoria, objetos: int = 2) -> None:
    """Um extrato da empresa com `arquivo_chave` e `objetos` arquivos no prefixo."""
    extrato = db_session.scalars(select(Extrato).where(Extrato.empresa_id == empresa.id)).first()
    extrato.arquivo_chave = chave_do_objeto(empresa.conta_id, empresa.id, extrato.id)
    db_session.commit()
    memoria.objetos[extrato.arquivo_chave] = b"cifrado"
    for _ in range(objetos - 1):
        memoria.objetos[f"{prefixo_da_empresa(empresa.conta_id, empresa.id)}{uuid.uuid4()}"] = (
            b"cifrado"
        )


def test_apaga_os_arquivos_da_empresa_e_nao_os_das_outras(
    db_session, conta_povoada, limpar_registros
):
    conta, _ = conta_povoada()
    vencida, ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, vencida, memoria, objetos=2)
    _com_original(db_session, ativa, memoria, objetos=3)
    de_outra_conta = f"contas/{uuid.uuid4()}/empresas/{uuid.uuid4()}/{uuid.uuid4()}"
    memoria.objetos[de_outra_conta] = b"cifrado"
    _remover(db_session, vencida.id)

    assert _rodar(armazenamento=memoria) == 0

    prefixo_ativa = prefixo_da_empresa(conta.id, ativa.id)
    assert sorted(memoria.objetos) == sorted(
        [de_outra_conta, *[k for k in memoria.objetos if k.startswith(prefixo_ativa)]]
    )
    assert sum(k.startswith(prefixo_ativa) for k in memoria.objetos) == 3
    (registro,) = _ids_dos_registros(db_session, vencida.id)
    assert registro.contagens["arquivos_originais_r2"] == 2


def test_falha_no_r2_nao_apaga_o_banco_daquela_empresa_e_as_outras_seguem(
    db_session, conta_povoada, limpar_registros, caplog
):
    conta, _ = conta_povoada()
    com_falha, boa = _empresas(db_session, conta.id)
    limpar_registros(com_falha.id)
    limpar_registros(boa.id)

    class _FalhaNaPrimeira(ArmazenamentoEmMemoria):
        def apagar_prefixo(self, prefixo: str) -> int:
            if str(com_falha.id) in prefixo:
                raise RuntimeError("https://abc.r2.cloudflarestorage.com fora do ar")
            return super().apagar_prefixo(prefixo)

    memoria = _FalhaNaPrimeira()
    _com_original(db_session, com_falha, memoria, objetos=1)
    _remover(db_session, com_falha.id, apagar_em=AGORA - timedelta(days=2))
    _remover(db_session, boa.id)
    antes = _contagens(db_session, conta.id, com_falha.id)
    caplog.set_level(logging.INFO)

    codigo = _rodar(armazenamento=memoria)

    assert codigo == 1
    assert _contagens(db_session, conta.id, com_falha.id) == antes
    assert [e.id for e in _empresas(db_session, conta.id)] == [com_falha.id]
    assert len(memoria.objetos) == 1
    assert "resultado=falha classe=RuntimeError" in caplog.text
    assert "r2.cloudflarestorage" not in caplog.text
    assert "apagadas=1 puladas=0 falhas=1" in caplog.text


def test_sem_r2_configurado_e_com_arquivo_original_pula_a_empresa(
    db_session, conta_povoada, limpar_registros, caplog
):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    _com_original(db_session, vencida, ArmazenamentoEmMemoria(), objetos=1)
    _remover(db_session, vencida.id)
    caplog.set_level(logging.INFO)

    codigo = _rodar(armazenamento=None)

    assert codigo == 1
    assert [e.id for e in _empresas(db_session, conta.id)] == [vencida.id, _ativa.id]
    assert "resultado=falha" in caplog.text


def test_sem_r2_configurado_e_sem_arquivo_original_segue(
    db_session, conta_povoada, limpar_registros
):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    limpar_registros(vencida.id)
    _remover(db_session, vencida.id)

    assert _rodar(armazenamento=None) == 0

    assert [e.id for e in _empresas(db_session, conta.id)] == [_ativa.id]
    (registro,) = _ids_dos_registros(db_session, vencida.id)
    assert "arquivos_originais_r2" not in registro.contagens


def test_falha_ao_criar_o_cliente_do_r2_nao_apaga_nada(db_session, conta_povoada, caplog):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    _remover(db_session, vencida.id)
    caplog.set_level(logging.INFO)

    def _falha():
        raise RuntimeError("https://abc.r2.cloudflarestorage.com fora do ar")

    codigo = script.main([], agora=lambda: AGORA, criar_armazenamento=_falha)

    assert codigo == 1
    assert len(_empresas(db_session, conta.id)) == 2
    assert "resultado=erro_r2 classe=RuntimeError" in caplog.text
    assert "r2.cloudflarestorage" not in caplog.text


# --- concorrência e falha no banco -----------------------------------------------------


def test_empresa_restaurada_depois_da_listagem_e_pulada_sem_tocar_no_r2(db_session, conta_povoada):
    conta, _ = conta_povoada()
    vencida, _ativa = _empresas(db_session, conta.id)
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, vencida, memoria, objetos=2)
    _remover(db_session, vencida.id)
    (par,) = [p for p in _vencidas(db_session) if p[1] == vencida.id]
    # Alguém restaurou entre a listagem e a exclusão.
    db_session.execute(
        update(Empresa)
        .where(Empresa.id == vencida.id)
        .values(removida_em=None, removida_por_id=None, removida_por_nome=None, apagar_em=None),
        execution_options={"synchronize_session": False},
    )
    db_session.commit()

    with script.SessionLocal() as outra:
        resultado = script.apagar_uma(outra, par[0], par[1], AGORA, memoria)

    assert resultado is None
    assert len(memoria.objetos) == 2
    assert _contagens(db_session, conta.id, vencida.id)["extratos"] == 2


def _vencidas(db_session):
    return servico.empresas_vencidas(db_session, AGORA)


def test_falha_no_banco_desfaz_so_aquela_empresa(
    db_session, conta_povoada, limpar_registros, monkeypatch, caplog
):
    conta, _ = conta_povoada()
    com_falha, boa = _empresas(db_session, conta.id)
    limpar_registros(com_falha.id)
    limpar_registros(boa.id)
    _remover(db_session, com_falha.id, apagar_em=AGORA - timedelta(days=2))
    _remover(db_session, boa.id)
    antes = _contagens(db_session, conta.id, com_falha.id)
    original = servico.apagar_linhas

    def _falha_na_primeira(db, escopo, etapas=ETAPAS):
        contagens = original(db, escopo, etapas)  # apaga tudo...
        if escopo.empresa_id == com_falha.id:
            raise RuntimeError("falha depois de apagar (teste)")  # ...e falha antes do commit
        return contagens

    monkeypatch.setattr(servico, "apagar_linhas", _falha_na_primeira)
    caplog.set_level(logging.INFO)

    assert _rodar() == 1

    assert _contagens(db_session, conta.id, com_falha.id) == antes
    assert [e.id for e in _empresas(db_session, conta.id)] == [com_falha.id]
    assert _ids_dos_registros(db_session, com_falha.id) == []
    assert "apagadas=1 puladas=0 falhas=1" in caplog.text


# --- ponta a ponta com a API -----------------------------------------------------------


def test_empresa_removida_pela_api_e_apagada_depois_dos_30_dias(
    db_session, criar_usuario, criar_empresa, gerar_token, limpar_registros
):
    admin = criar_usuario(nome="Ana Admin")
    cliente = criar_empresa("Cliente Que Sai Ltda", conta_id=admin.conta_id)
    cliente_id = cliente.id
    limpar_registros(cliente_id)
    headers = cabecalhos(gerar_token(admin.conta_id, usuario_id=admin.id))
    corpo = client.delete(f"/empresas/{cliente_id}", headers=headers).json()
    apagar_em = datetime.fromisoformat(corpo["apagar_em"])

    assert _rodar(agora=apagar_em - timedelta(minutes=1)) == 0
    assert client.post(f"/empresas/{cliente_id}/restaurar", headers=headers).status_code == 200
    client.delete(f"/empresas/{cliente_id}", headers=headers)
    apagar_em = db_session.scalar(
        select(Empresa.apagar_em)
        .where(Empresa.id == cliente_id)
        .execution_options(incluir_removidas=True)
    )

    assert _rodar(agora=apagar_em + timedelta(seconds=1)) == 0

    assert [e.id for e in _empresas(db_session, admin.conta_id)] != []
    assert cliente_id not in {e.id for e in _empresas(db_session, admin.conta_id)}
    assert client.get("/empresas/removidas", headers=headers).json() == {"itens": []}
    assert client.post(f"/empresas/{cliente_id}/restaurar", headers=headers).status_code == 404


# --- a exclusão da conta enxerga as empresas removidas ----------------------------------


def test_a_exclusao_da_conta_leva_tambem_a_empresa_removida_e_o_original_dela(
    db_session, conta_povoada, registros
):
    conta, linhas = conta_povoada()
    removida, _ativa = _empresas(db_session, conta.id)
    registros(conta.id)
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, removida, memoria, objetos=2)
    _remover(db_session, removida.id, apagar_em=AGORA + timedelta(days=10))
    conta_id = conta.id

    # Simulação: conta a empresa removida e o original dela.
    assert excluir_empresa.contar_linhas(db_session, conta_id)["empresas"] == 2
    assert excluir_empresa._extratos_com_original(db_session, conta_id) == 1

    exclusao = excluir_empresa.excluir_conta(db_session, conta_id, "teste")

    assert exclusao.contagens["empresas"] == 2
    assert exclusao.contagens["extratos"] == 4
    assert _contagens(db_session, conta_id) == dict.fromkeys(servico.tabelas_apagadas(), 0)
    assert len(_existentes(db_session, linhas)) == 0
