"""scripts/excluir_empresa.py (issue #68, ADR-017): exclusão dos dados de uma
empresa, a pedido, por processo interno.

Roda contra o Postgres local da suíte (o conftest recusa qualquer outro). O
que o script pede digitado vem de `perguntar`, trocado aqui por respostas
fixas.
"""

import hashlib
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import senha
from app.core.config import settings
from app.models import (
    Base,
    Conciliacao,
    Configuracao,
    Convite,
    DecisaoLinha,
    Empresa,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    RegistroExclusao,
    TokenEmail,
    Usuario,
)
from scripts import excluir_empresa as script

NOME_ADMIN = "Ana Administradora"
NOME_CONVIDADO = "Bruno Convidado"


def _povoar(db_session, empresa: Empresa) -> list[tuple[type[Base], uuid.UUID]]:
    """Uma linha (pelo menos) em cada tabela ligada à empresa. Devolve
    (model, id) de tudo o que criou."""
    sufixo = uuid.uuid4().hex[:10]
    admin = Usuario(
        empresa_id=empresa.id,
        nome=NOME_ADMIN,
        email=f"admin-{sufixo}@teste.com",
        senha_hash=senha.gerar_hash("senha-forte-123"),
        administrador=True,
    )
    convidado = Usuario(
        empresa_id=empresa.id,
        nome=NOME_CONVIDADO,
        email=f"convidado-{sufixo}@teste.com",
        google_sub=f"google-{sufixo}",
    )
    db_session.add_all([admin, convidado])
    db_session.flush()

    banco = Extrato(
        empresa_id=empresa.id,
        nome_arquivo="banco.ofx",
        formato="ofx",
        tamanho_bytes=10,
        origem="banco",
        status="concluido",
    )
    sistema = Extrato(
        empresa_id=empresa.id,
        nome_arquivo="sistema.csv",
        formato="csv",
        tamanho_bytes=10,
        origem="sistema",
        status="concluido",
    )
    db_session.add_all([banco, sistema])
    db_session.flush()

    def _lancamento(extrato: Extrato) -> Lancamento:
        return Lancamento(
            empresa_id=empresa.id,
            extrato_id=extrato.id,
            data=date(2026, 9, 5),
            valor=Decimal("10.00"),
            descricao="PIX MARIA",
            tipo="credito",
            hash_dedup=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        )

    lancamento_banco, lancamento_sistema = _lancamento(banco), _lancamento(sistema)
    db_session.add_all([lancamento_banco, lancamento_sistema])
    db_session.flush()

    linhas = [
        admin,
        convidado,
        banco,
        sistema,
        lancamento_banco,
        lancamento_sistema,
        LinhaInvalida(extrato_id=banco.id, identificador="linha 3", motivo="data inválida"),
        Conciliacao(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            lancamento_banco_id=lancamento_banco.id,
            lancamento_sistema_id=lancamento_sistema.id,
            status="match_exato",
        ),
        ExecucaoConciliacao(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            tolerancia_dias=0,
            total=1,
            match_exato=1,
            match_tolerancia=0,
            duplicado=0,
            sem_correspondencia=0,
            tarifa_bancaria=0,
            divergente_valor=0,
            divergente_data=0,
        ),
        DecisaoLinha(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            chave="c" * 64,
            tipo="conferida",
            usuario_id=convidado.id,
            autor_nome=NOME_CONVIDADO,
            rodada=1,
            extrato_sistema_id=sistema.id,
        ),
        Fechamento(
            empresa_id=empresa.id,
            competencia="2026-09",
            resumo={},
            fechado_por_id=admin.id,
            fechado_por_nome=NOME_ADMIN,
        ),
        ExplicacaoDivergencia(
            empresa_id=empresa.id,
            chave=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            status="gerada",
            provedor="falso",
            modelo="falso",
            versao_prompt="1",
            texto="Explicação de teste.",
            tokens_entrada=1,
            tokens_saida=1,
        ),
        Convite(
            empresa_id=empresa.id,
            email=f"convite-{sufixo}@teste.com",
            token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            convidado_por_id=admin.id,
            expira_em=func.now() + timedelta(days=7),
        ),
        TokenEmail(
            usuario_id=admin.id,
            finalidade="recuperacao_senha",
            token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            expira_em=func.now() + timedelta(minutes=30),
        ),
        Configuracao(empresa_id=empresa.id, atualizado_por_id=admin.id),
    ]
    db_session.add_all(linhas[6:])
    db_session.commit()
    return [(Empresa, empresa.id)] + [(type(linha), linha.id) for linha in linhas]


@pytest.fixture
def empresa_povoada(db_session, criar_empresa):
    """Empresa com linha em todas as tabelas. A limpeza de `criar_empresa`
    apaga o que sobrar (a da empresa que o teste não excluiu)."""

    def _criar(razao_social: str = "Empresa a Excluir Ltda"):
        empresa = criar_empresa(razao_social)
        return empresa, _povoar(db_session, empresa)

    return _criar


@pytest.fixture
def registros(db_session):
    """Apaga, no fim, os registros de exclusão das empresas do teste."""
    empresa_ids: list[uuid.UUID] = []
    yield empresa_ids.append
    db_session.rollback()
    db_session.query(RegistroExclusao).filter(RegistroExclusao.empresa_id.in_(empresa_ids)).delete(
        synchronize_session=False
    )
    db_session.commit()


def _existentes(db_session, linhas) -> list[tuple[str, uuid.UUID]]:
    db_session.expire_all()
    return [
        (model.__tablename__, id_)
        for model, id_ in linhas
        if db_session.get(model, id_) is not None
    ]


class _Respostas:
    """Faz o papel do `input`: devolve as respostas na ordem e guarda as
    perguntas."""

    def __init__(self, *respostas: str) -> None:
        self._respostas = list(respostas)
        self.perguntas: list[str] = []

    def __call__(self, pergunta: str) -> str:
        self.perguntas.append(pergunta)
        return self._respostas.pop(0)


# --- cobertura do schema -----------------------------------------------------


def _tabelas_ligadas_a_empresa() -> set[str]:
    """Toda tabela com coluna `empresa_id`, ou com FK (direta ou não) para
    uma tabela ligada à empresa, mais a própria `empresas`."""
    ligadas = {Empresa.__tablename__}
    mudou = True
    while mudou:
        mudou = False
        for tabela in Base.metadata.tables.values():
            if tabela.name in ligadas:
                continue
            if "empresa_id" in tabela.c or any(
                fk.column.table.name in ligadas for fk in tabela.foreign_keys
            ):
                ligadas.add(tabela.name)
                mudou = True
    return ligadas


def test_script_apaga_toda_tabela_ligada_a_empresa():
    """Uma tabela nova com `empresa_id`, ou com FK para uma tabela da
    empresa, faz este teste falhar até entrar em `ETAPAS`."""
    apagadas = script.tabelas_apagadas()

    assert len(apagadas) == len(set(apagadas))
    assert set(apagadas) == _tabelas_ligadas_a_empresa() - script.TABELAS_QUE_FICAM
    assert script.TABELAS_QUE_FICAM == {"registros_exclusao"}
    assert not Base.metadata.tables["registros_exclusao"].foreign_keys


def test_ordem_das_etapas_respeita_as_fks():
    """Quem aponta para uma tabela sai antes dela."""
    ordem = {tabela: posicao for posicao, tabela in enumerate(script.tabelas_apagadas())}

    for tabela, posicao in ordem.items():
        for fk in Base.metadata.tables[tabela].foreign_keys:
            referenciada = fk.column.table.name
            if referenciada != tabela:
                assert posicao < ordem[referenciada], f"{tabela} sai depois de {referenciada}"


# --- exclusão ----------------------------------------------------------------


def test_exclusao_apaga_tudo_da_empresa_e_nada_da_outra(db_session, empresa_povoada, registros):
    empresa, linhas = empresa_povoada()
    _, linhas_da_outra = empresa_povoada("Outra Empresa Ltda")
    empresa_id, cnpj = empresa.id, empresa.cnpj
    registros(empresa_id)

    exclusao = script.excluir_empresa(db_session, empresa_id, "rafael (teste)")

    assert _existentes(db_session, linhas) == []
    assert len(_existentes(db_session, linhas_da_outra)) == len(linhas_da_outra)
    # Nenhuma tabela da lista ficou sem linha no teste: a exclusão passou por todas.
    assert list(exclusao.contagens) == script.tabelas_apagadas()
    assert all(quantidade >= 1 for quantidade in exclusao.contagens.values())
    assert exclusao.contagens["usuarios"] == 2
    assert exclusao.contagens["empresas"] == 1
    assert sum(exclusao.contagens.values()) == len(linhas)
    assert script.contar_linhas(db_session, empresa_id) == dict.fromkeys(
        script.tabelas_apagadas(), 0
    )

    db_session.expire_all()
    gravado = db_session.get(RegistroExclusao, exclusao.registro_id)
    assert gravado.empresa_id == empresa_id
    assert gravado.contagens == exclusao.contagens
    assert gravado.cnpj_sha256 == hashlib.sha256(cnpj.encode()).hexdigest()
    assert gravado.executado_por == "rafael (teste)"
    assert gravado.executado_em is not None
    # Nada dos dados apagados no registro.
    conteudo = json.dumps(gravado.contagens) + gravado.cnpj_sha256
    for texto in [cnpj, "Empresa a Excluir", NOME_ADMIN, NOME_CONVIDADO, "@teste.com"]:
        assert texto not in conteudo
    assert {c.name for c in RegistroExclusao.__table__.columns} == {
        "id",
        "empresa_id",
        "cnpj_sha256",
        "contagens",
        "executado_em",
        "executado_por",
    }


def test_falha_no_meio_nao_apaga_nada(db_session, empresa_povoada, monkeypatch):
    empresa, linhas = empresa_povoada()

    def _falha(empresa_id):
        raise RuntimeError("falha no meio (teste)")

    etapas = list(script.ETAPAS)
    etapas[7] = (etapas[7][0], _falha)  # extratos, depois de várias tabelas apagadas
    monkeypatch.setattr(script, "ETAPAS", tuple(etapas))

    with pytest.raises(RuntimeError):
        script.excluir_empresa(db_session, empresa.id, "teste")

    assert len(_existentes(db_session, linhas)) == len(linhas)
    assert (
        db_session.scalar(select(func.count()).where(RegistroExclusao.empresa_id == empresa.id))
        == 0
    )


# --- linha de comando --------------------------------------------------------


def test_simulacao_mostra_as_contagens_e_nao_apaga_nada(db_session, empresa_povoada, capsys):
    empresa, linhas = empresa_povoada()
    perguntar = _Respostas()

    codigo = script.main(["--empresa-id", str(empresa.id)], perguntar=perguntar)

    saida = capsys.readouterr().out
    assert codigo == 0
    assert perguntar.perguntas == []
    assert len(_existentes(db_session, linhas)) == len(linhas)
    assert "Empresa a Excluir Ltda" in saida
    assert f"{empresa.cnpj[:2]}.***.***/****-{empresa.cnpj[-2:]}" in saida
    assert empresa.cnpj not in saida
    assert "Usuários ativos: 2" in saida
    for tabela in script.tabelas_apagadas():
        assert tabela in saida
    assert "Simulação: nada foi apagado." in saida
    for texto in [NOME_ADMIN, NOME_CONVIDADO, "@teste.com"]:
        assert texto not in saida


def test_executar_com_o_cnpj_certo_apaga(db_session, empresa_povoada, registros, capsys):
    empresa, linhas = empresa_povoada()
    empresa_id, cnpj = empresa.id, empresa.cnpj
    registros(empresa_id)
    formatado = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"

    codigo = script.main(
        ["--empresa-id", str(empresa_id), "--executar"],
        perguntar=_Respostas("rafael", formatado),
    )

    saida = capsys.readouterr().out
    assert codigo == 0
    assert _existentes(db_session, linhas) == []
    assert "Empresa excluída." in saida
    registro = db_session.scalars(
        select(RegistroExclusao).where(RegistroExclusao.empresa_id == empresa_id)
    ).one()
    assert str(registro.id) in saida
    assert registro.executado_por == "rafael"
    for texto in [NOME_ADMIN, NOME_CONVIDADO, "@teste.com", cnpj]:
        assert texto not in saida


@pytest.mark.parametrize(
    "respostas", [("rafael", "00000000000000"), ("", "qualquer")], ids=["cnpj_errado", "sem_quem"]
)
def test_executar_sem_a_confirmacao_nao_apaga(db_session, empresa_povoada, respostas, capsys):
    empresa, linhas = empresa_povoada()

    codigo = script.main(
        ["--empresa-id", str(empresa.id), "--executar"], perguntar=_Respostas(*respostas)
    )

    assert codigo == 1
    assert "Nada foi apagado." in capsys.readouterr().out
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_empresa_inexistente(db_session, capsys):
    codigo = script.main(["--empresa-id", str(uuid.uuid4())], perguntar=_Respostas())

    assert codigo == 1
    assert "não encontrada" in capsys.readouterr().out


# --- guarda de banco ---------------------------------------------------------

URL_REMOTA = "postgresql+psycopg://ledgr:segredo@banco.railway.example:5432/railway"


class _ConectouNoRemoto(Exception):
    pass


@pytest.fixture
def banco_remoto(monkeypatch):
    """DATABASE_URL remota, e nenhuma conexão: se o script passar da guarda,
    `_criar_engine` levanta `_ConectouNoRemoto`."""
    monkeypatch.setattr(settings, "database_url", URL_REMOTA)
    tentativas: list[str] = []

    def _criar_engine(url):
        tentativas.append(url)
        raise _ConectouNoRemoto

    monkeypatch.setattr(script, "_criar_engine", _criar_engine)
    return tentativas


@pytest.mark.parametrize("executar", [False, True], ids=["simulacao", "executar"])
def test_banco_remoto_sem_a_flag_e_recusado(banco_remoto, capsys, executar):
    argumentos = ["--empresa-id", str(uuid.uuid4())] + (["--executar"] if executar else [])
    perguntar = _Respostas()

    assert script.main(argumentos, perguntar=perguntar) == 1

    assert banco_remoto == []
    assert perguntar.perguntas == []
    saida = capsys.readouterr().out
    assert "Recusado" in saida
    assert "segredo" not in saida


def test_banco_remoto_com_a_flag_e_o_host_errado_e_recusado(banco_remoto, capsys):
    perguntar = _Respostas("localhost")

    codigo = script.main(["--empresa-id", str(uuid.uuid4()), "--banco-remoto"], perguntar=perguntar)

    assert codigo == 1
    assert banco_remoto == []
    assert len(perguntar.perguntas) == 1
    assert "Nada foi feito." in capsys.readouterr().out


def test_banco_remoto_com_a_flag_e_o_host_digitado_passa_da_guarda(banco_remoto):
    with pytest.raises(_ConectouNoRemoto):
        script.main(
            ["--empresa-id", str(uuid.uuid4()), "--banco-remoto"],
            perguntar=_Respostas("banco.railway.example"),
        )

    assert banco_remoto == [URL_REMOTA]
