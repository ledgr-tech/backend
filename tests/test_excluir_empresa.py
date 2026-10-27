"""scripts/excluir_empresa.py (issues #68 e #133, ADR-017): exclusão dos dados
de uma conta inteira, com todas as empresas clientes, a pedido, por processo
interno.

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
from conftest import ArmazenamentoEmMemoria, _gerar_cnpj_alfanumerico
from sqlalchemy import func, select

from app.core import senha
from app.core.config import settings
from app.models import (
    Base,
    ChaveApi,
    ClienteOAuth,
    CodigoOAuth,
    Conciliacao,
    ConexaoMcp,
    Configuracao,
    Conta,
    Convite,
    DecisaoLinha,
    Empresa,
    EmpresaFavorita,
    ExecucaoConciliacao,
    ExplicacaoDivergencia,
    Extrato,
    Fechamento,
    Lancamento,
    LinhaInvalida,
    PedidoAutorizacaoOAuth,
    RegistroExclusao,
    TokenEmail,
    TokenOAuth,
    UsoAssistente,
    Usuario,
)
from app.services.arquivo_original import chave_do_objeto, prefixo_da_conta
from scripts import excluir_empresa as script

NOME_ADMIN = "Ana Administradora"
NOME_CONVIDADO = "Bruno Convidado"


def _povoar_empresa(db_session, empresa: Empresa, admin: Usuario, convidado: Usuario) -> list:
    """Uma linha (pelo menos) em cada tabela por `empresa_id`. Devolve as
    linhas criadas, já gravadas."""
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
        Configuracao(empresa_id=empresa.id, atualizado_por_id=admin.id),
    ]
    db_session.add_all(linhas)
    db_session.flush()
    return [banco, sistema, lancamento_banco, lancamento_sistema, *linhas]


def _povoar_conexao_mcp(db_session, conta: Conta, empresa: Empresa, admin: Usuario) -> list:
    """Pedido, conexão, código e token OAuth da conta (issue #136). O cliente
    OAuth não é da conta: fica, e a limpeza do conftest o apaga."""

    def _hash() -> str:
        return hashlib.sha256(uuid.uuid4().bytes).hexdigest()

    cliente = ClienteOAuth(
        client_id=uuid.uuid4().hex, client_name="Claude", redirect_uris=["https://claude.ai/cb"]
    )
    db_session.add(cliente)
    db_session.flush()
    comum = {
        "client_id": cliente.client_id,
        "scope": "ledgr.leitura",
        "resource": "https://api.teste/mcp",
    }
    pedido = PedidoAutorizacaoOAuth(
        hash=_hash(),
        redirect_uri="https://claude.ai/cb",
        code_challenge="x" * 43,
        expira_em=func.now(),
        conta_id=conta.id,
        **comum,
    )
    conexao = ConexaoMcp(
        conta_id=conta.id,
        empresa_id=empresa.id,
        client_id=cliente.client_id,
        nome_cliente="Claude",
        escopo="ledgr.leitura",
        criada_por_id=admin.id,
    )
    db_session.add_all([pedido, conexao])
    db_session.flush()
    codigo = CodigoOAuth(
        hash=_hash(),
        conexao_id=conexao.id,
        redirect_uri="https://claude.ai/cb",
        code_challenge="x" * 43,
        expira_em=func.now(),
        **comum,
    )
    db_session.add(codigo)
    db_session.flush()
    token = TokenOAuth(
        conexao_id=conexao.id,
        codigo_id=codigo.id,
        hash_acesso=_hash(),
        acesso_expira_em=func.now(),
        **comum,
    )
    db_session.add(token)
    db_session.flush()
    return [pedido, conexao, codigo, token]


def _povoar(
    db_session, conta: Conta, empresas: list[Empresa]
) -> list[tuple[type[Base], uuid.UUID]]:
    """Usuários, convite e link de e-mail da conta, e uma linha (pelo menos)
    em cada tabela de cada empresa dela. Devolve (model, id) de tudo, a conta
    e as empresas inclusive."""
    sufixo = uuid.uuid4().hex[:10]
    admin = Usuario(
        conta_id=conta.id,
        nome=NOME_ADMIN,
        email=f"admin-{sufixo}@teste.com",
        senha_hash=senha.gerar_hash("senha-forte-123"),
        papel="admin",
    )
    convidado = Usuario(
        conta_id=conta.id,
        nome=NOME_CONVIDADO,
        email=f"convidado-{sufixo}@teste.com",
        google_sub=f"google-{sufixo}",
        papel="analista",
    )
    db_session.add_all([admin, convidado])
    db_session.flush()

    linhas = [
        admin,
        convidado,
        Convite(
            conta_id=conta.id,
            papel="analista",
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
        UsoAssistente(conta_id=conta.id),
        ChaveApi(
            conta_id=conta.id,
            empresa_id=empresas[0].id,
            nome="Integração",
            prefixo="abcdefgh",
            hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            criada_por_id=admin.id,
        ),
    ]
    db_session.add_all(linhas[2:])
    linhas += _povoar_conexao_mcp(db_session, conta, empresas[0], admin)
    for empresa in empresas:
        linhas += _povoar_empresa(db_session, empresa, admin, convidado)
    # Favoritas (issue #157): a PK é composta, então entram com a tupla.
    favoritas = [
        EmpresaFavorita(usuario_id=admin.id, empresa_id=empresa.id, conta_id=conta.id)
        for empresa in empresas
    ]
    db_session.add_all(favoritas)
    db_session.commit()
    return (
        [(Conta, conta.id)]
        + [(Empresa, empresa.id) for empresa in empresas]
        + [(type(linha), linha.id) for linha in linhas]
        + [(EmpresaFavorita, (f.usuario_id, f.empresa_id)) for f in favoritas]
    )


@pytest.fixture
def conta_povoada(db_session, criar_empresa):
    """Conta com duas empresas clientes (issue #133) e linha em todas as
    tabelas. A limpeza das fábricas apaga o que sobrar (a da conta que o teste
    não excluiu). `cnpj_cliente` dá o CNPJ da segunda empresa."""

    def _criar(razao_social: str = "Empresa a Excluir Ltda", cnpj_cliente: str | None = None):
        primeira = criar_empresa(razao_social)
        conta = db_session.get(Conta, primeira.conta_id)
        segunda = criar_empresa("Cliente do Escritório", conta_id=conta.id, cnpj=cnpj_cliente)
        return conta, _povoar(db_session, conta, [primeira, segunda])

    return _criar


@pytest.fixture
def registros(db_session):
    """Apaga, no fim, os registros de exclusão das contas do teste."""
    conta_ids: list[uuid.UUID] = []
    yield conta_ids.append
    db_session.rollback()
    db_session.query(RegistroExclusao).filter(RegistroExclusao.empresa_id.in_(conta_ids)).delete(
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


def _tabelas_ligadas_a_conta() -> set[str]:
    """Toda tabela com coluna `conta_id` ou `empresa_id`, ou com FK (direta ou
    não) para uma tabela ligada à conta, mais a própria `contas`."""
    ligadas = {Conta.__tablename__}
    mudou = True
    while mudou:
        mudou = False
        for tabela in Base.metadata.tables.values():
            if tabela.name in ligadas:
                continue
            if (
                "conta_id" in tabela.c
                or "empresa_id" in tabela.c
                or any(fk.column.table.name in ligadas for fk in tabela.foreign_keys)
            ):
                ligadas.add(tabela.name)
                mudou = True
    return ligadas


def test_script_apaga_toda_tabela_ligada_a_conta():
    """Uma tabela nova com `conta_id` ou `empresa_id`, ou com FK para uma
    tabela da conta, faz este teste falhar até entrar em `ETAPAS`."""
    apagadas = script.tabelas_apagadas()

    assert len(apagadas) == len(set(apagadas))
    assert set(apagadas) == _tabelas_ligadas_a_conta() - script.TABELAS_QUE_FICAM
    assert script.TABELAS_QUE_FICAM == {"registros_exclusao"}
    assert not Base.metadata.tables["registros_exclusao"].foreign_keys


def test_ordem_das_etapas_respeita_as_fks():
    """Quem aponta para uma tabela que também sai, sai antes dela. As que ficam
    (`clientes_oauth`, que não é de nenhuma conta) não entram na ordem."""
    ordem = {tabela: posicao for posicao, tabela in enumerate(script.tabelas_apagadas())}

    for tabela, posicao in ordem.items():
        for fk in Base.metadata.tables[tabela].foreign_keys:
            referenciada = fk.column.table.name
            if referenciada != tabela and referenciada in ordem:
                assert posicao < ordem[referenciada], f"{tabela} sai depois de {referenciada}"


# --- exclusão ----------------------------------------------------------------


def test_exclusao_apaga_a_conta_inteira_e_nada_da_outra(db_session, conta_povoada, registros):
    """As duas empresas da conta saem; a outra conta fica inteira, inclusive a
    empresa cliente dela com o mesmo CNPJ de uma empresa da conta excluída."""
    conta, linhas = conta_povoada()
    cnpj_cliente = db_session.scalars(
        select(Empresa.cnpj).where(Empresa.conta_id == conta.id, Empresa.cnpj != conta.cnpj)
    ).one()
    _, linhas_da_outra = conta_povoada("Outra Empresa Ltda", cnpj_cliente=cnpj_cliente)
    conta_id, cnpj = conta.id, conta.cnpj
    registros(conta_id)

    exclusao = script.excluir_conta(db_session, conta_id, "rafael (teste)")

    assert _existentes(db_session, linhas) == []
    assert len(_existentes(db_session, linhas_da_outra)) == len(linhas_da_outra)
    # Nenhuma tabela da lista ficou sem linha no teste: a exclusão passou por todas.
    assert list(exclusao.contagens) == script.tabelas_apagadas()
    assert all(quantidade >= 1 for quantidade in exclusao.contagens.values())
    assert exclusao.contagens["usuarios"] == 2
    assert exclusao.contagens["empresas"] == 2
    assert exclusao.contagens["extratos"] == 4
    assert exclusao.contagens["contas"] == 1
    assert sum(exclusao.contagens.values()) == len(linhas)
    assert script.contar_linhas(db_session, conta_id) == dict.fromkeys(script.tabelas_apagadas(), 0)

    db_session.expire_all()
    gravado = db_session.get(RegistroExclusao, exclusao.registro_id)
    # A coluna se chama `empresa_id`, mas guarda o id da conta (issue #133).
    assert gravado.empresa_id == conta_id
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


def test_falha_no_meio_nao_apaga_nada(db_session, conta_povoada, monkeypatch):
    conta, linhas = conta_povoada()

    def _falha(conta_id):
        raise RuntimeError("falha no meio (teste)")

    etapas = list(script.ETAPAS)
    etapas[7] = (etapas[7][0], _falha)  # extratos, depois de várias tabelas apagadas
    monkeypatch.setattr(script, "ETAPAS", tuple(etapas))

    with pytest.raises(RuntimeError):
        script.excluir_conta(db_session, conta.id, "teste")

    assert len(_existentes(db_session, linhas)) == len(linhas)
    assert (
        db_session.scalar(select(func.count()).where(RegistroExclusao.empresa_id == conta.id)) == 0
    )


# --- linha de comando --------------------------------------------------------


def test_simulacao_mostra_as_contagens_e_nao_apaga_nada(db_session, conta_povoada, capsys):
    conta, linhas = conta_povoada()
    perguntar = _Respostas()

    codigo = script.main(["--conta-id", str(conta.id)], perguntar=perguntar)

    saida = capsys.readouterr().out
    assert codigo == 0
    assert perguntar.perguntas == []
    assert len(_existentes(db_session, linhas)) == len(linhas)
    assert "Empresa a Excluir Ltda" in saida
    assert f"{conta.cnpj[:2]}.***.***/****-{conta.cnpj[-2:]}" in saida
    assert conta.cnpj not in saida
    assert "Empresas:        2" in saida
    assert "Usuários ativos: 2" in saida
    for tabela in script.tabelas_apagadas():
        assert tabela in saida
    assert "Simulação: nada foi apagado." in saida
    for texto in [NOME_ADMIN, NOME_CONVIDADO, "@teste.com"]:
        assert texto not in saida


def _com_mascara(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def _conta_com_cnpj(db_session, conta_povoada, tipo: str):
    conta, linhas = conta_povoada()
    if tipo == "alfanumerico":
        conta.cnpj = _gerar_cnpj_alfanumerico()
        db_session.commit()
    return conta, linhas


@pytest.mark.parametrize("tipo", ["numerico", "alfanumerico"])
@pytest.mark.parametrize(
    "digitar",
    [_com_mascara, lambda cnpj: cnpj, lambda cnpj: f"  {_com_mascara(cnpj).lower()} "],
    ids=["com_mascara", "sem_mascara", "minusculas_e_espacos"],
)
def test_executar_com_o_cnpj_certo_apaga(
    db_session, conta_povoada, registros, capsys, tipo, digitar
):
    conta, linhas = _conta_com_cnpj(db_session, conta_povoada, tipo)
    conta_id, cnpj = conta.id, conta.cnpj
    registros(conta_id)
    assert cnpj.isdigit() is (tipo == "numerico")

    codigo = script.main(
        ["--conta-id", str(conta_id), "--executar"],
        perguntar=_Respostas("rafael", digitar(cnpj)),
    )

    saida = capsys.readouterr().out
    assert codigo == 0
    assert _existentes(db_session, linhas) == []
    assert "Conta excluída." in saida
    registro = db_session.scalars(
        select(RegistroExclusao).where(RegistroExclusao.empresa_id == conta_id)
    ).one()
    assert str(registro.id) in saida
    assert registro.executado_por == "rafael"
    for texto in [NOME_ADMIN, NOME_CONVIDADO, "@teste.com", cnpj]:
        assert texto not in saida


@pytest.mark.parametrize("tipo", ["numerico", "alfanumerico"])
@pytest.mark.parametrize(
    "respostas",
    [
        lambda cnpj: ("rafael", "00000000000000"),
        lambda cnpj: ("rafael", "12.345.678/0001-95"),
        lambda cnpj: ("rafael", _gerar_cnpj_alfanumerico()),
        lambda cnpj: ("rafael", cnpj[:-1] + str((int(cnpj[-1]) + 1) % 10)),
        lambda cnpj: ("rafael", ""),
        lambda cnpj: ("", cnpj),
    ],
    ids=[
        "zeros",
        "outro_numerico",
        "outro_alfanumerico",
        "verificador_errado",
        "vazio",
        "sem_quem",
    ],
)
def test_executar_sem_a_confirmacao_nao_apaga(db_session, conta_povoada, respostas, capsys, tipo):
    conta, linhas = _conta_com_cnpj(db_session, conta_povoada, tipo)

    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=_Respostas(*respostas(conta.cnpj)),
    )

    assert codigo == 1
    assert "Nada foi apagado." in capsys.readouterr().out
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_cnpj_alfanumerico_sem_as_letras_nao_confere(db_session, conta_povoada, capsys):
    """O que o script fazia antes com o digitado: tirar tudo que não é dígito."""
    conta, linhas = _conta_com_cnpj(db_session, conta_povoada, "alfanumerico")
    so_os_digitos = "".join(c for c in conta.cnpj if c.isdigit())

    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=_Respostas("rafael", so_os_digitos),
    )

    assert codigo == 1
    assert "Nada foi apagado." in capsys.readouterr().out
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_conta_inexistente(db_session, capsys):
    codigo = script.main(["--conta-id", str(uuid.uuid4())], perguntar=_Respostas())

    assert codigo == 1
    assert "não encontrada" in capsys.readouterr().out


def test_id_de_empresa_cliente_nao_e_aceito_como_conta(db_session, conta_povoada, capsys):
    """Excluir uma empresa cliente sozinha fica para a #138: o id dela não
    é o de uma conta, e nada é apagado."""
    conta, linhas = conta_povoada()
    cliente = db_session.scalars(
        select(Empresa.id).where(Empresa.conta_id == conta.id, Empresa.cnpj != conta.cnpj)
    ).one()

    codigo = script.main(["--conta-id", str(cliente), "--executar"], perguntar=_Respostas())

    assert codigo == 1
    assert "não encontrada" in capsys.readouterr().out
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_empresa_id_continua_aceito_como_nome_antigo(db_session, conta_povoada, capsys):
    conta, _ = conta_povoada()

    assert script.main(["--empresa-id", str(conta.id)], perguntar=_Respostas()) == 0
    assert "Simulação: nada foi apagado." in capsys.readouterr().out


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


# --- arquivo original no R2 (issue #139, ADR-023) ------------------------------


def _com_original(db_session, conta: Conta, armazenamento=None, objetos: int = 0) -> None:
    """Marca um extrato da conta com `arquivo_chave` e, com `armazenamento`,
    grava `objetos` objetos no prefixo da conta (o do extrato entre eles)."""
    extrato = db_session.scalars(
        select(Extrato)
        .join(Empresa, Extrato.empresa_id == Empresa.id)
        .where(Empresa.conta_id == conta.id)
    ).first()
    extrato.arquivo_chave = chave_do_objeto(conta.id, extrato.empresa_id, extrato.id)
    db_session.commit()
    if armazenamento is None:
        return
    armazenamento.objetos[extrato.arquivo_chave] = b"cifrado"
    for _ in range(objetos - 1):
        chave = f"{prefixo_da_conta(conta.id)}empresas/{extrato.empresa_id}/{uuid.uuid4()}"
        armazenamento.objetos[chave] = b"cifrado"


class _FalhaAoApagar(ArmazenamentoEmMemoria):
    def apagar_prefixo(self, prefixo: str) -> int:
        raise RuntimeError("https://abc.r2.cloudflarestorage.com fora do ar")


def _registro(db_session, conta_id) -> RegistroExclusao:
    return db_session.scalars(
        select(RegistroExclusao).where(RegistroExclusao.empresa_id == conta_id)
    ).one()


def test_executar_apaga_os_arquivos_da_conta_e_nao_os_de_outra(
    db_session, conta_povoada, registros, capsys
):
    conta, linhas = conta_povoada()
    conta_id, cnpj = conta.id, conta.cnpj
    registros(conta_id)
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, conta, memoria, objetos=2)
    de_outra = f"contas/{uuid.uuid4()}/empresas/{uuid.uuid4()}/{uuid.uuid4()}"
    memoria.objetos[de_outra] = b"cifrado"

    codigo = script.main(
        ["--conta-id", str(conta_id), "--executar"],
        perguntar=_Respostas("vitor", cnpj),
        criar_armazenamento=lambda: memoria,
    )

    saida = capsys.readouterr().out
    assert codigo == 0
    assert list(memoria.objetos) == [de_outra]
    assert _existentes(db_session, linhas) == []
    assert _registro(db_session, conta_id).contagens["arquivos_originais_r2"] == 2
    assert "Arquivos originais no R2: 2" in saida
    assert prefixo_da_conta(conta_id) not in saida


def test_simulacao_mostra_os_arquivos_e_nao_apaga(db_session, conta_povoada, capsys):
    conta, linhas = conta_povoada()
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, conta, memoria, objetos=2)

    codigo = script.main(
        ["--conta-id", str(conta.id)], perguntar=_Respostas(), criar_armazenamento=lambda: memoria
    )

    assert codigo == 0
    assert "Arquivos originais no R2: 2" in capsys.readouterr().out
    assert len(memoria.objetos) == 2
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_simulacao_sem_variaveis_e_com_original_avisa(db_session, conta_povoada, capsys):
    conta, _ = conta_povoada()
    _com_original(db_session, conta)

    codigo = script.main(
        ["--conta-id", str(conta.id)], perguntar=_Respostas(), criar_armazenamento=lambda: None
    )

    assert codigo == 0
    assert "ARQUIVO_ORIGINAL_R2_*" in capsys.readouterr().out


def test_sem_variaveis_e_com_original_recusa_sem_perguntar(db_session, conta_povoada, capsys):
    conta, linhas = conta_povoada()
    _com_original(db_session, conta)
    perguntar = _Respostas()

    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=perguntar,
        criar_armazenamento=lambda: None,
    )

    assert codigo == 1
    assert perguntar.perguntas == []
    assert (
        "Sem as variáveis ARQUIVO_ORIGINAL_R2_* no ambiente, os arquivos originais ficariam "
        "no R2. Nada foi apagado." in capsys.readouterr().out
    )
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_sem_variaveis_e_sem_original_segue(db_session, conta_povoada, registros):
    conta, linhas = conta_povoada()
    conta_id, cnpj = conta.id, conta.cnpj
    registros(conta_id)

    codigo = script.main(
        ["--conta-id", str(conta_id), "--executar"],
        perguntar=_Respostas("vitor", cnpj),
        criar_armazenamento=lambda: None,
    )

    assert codigo == 0
    assert _existentes(db_session, linhas) == []
    assert "arquivos_originais_r2" not in _registro(db_session, conta_id).contagens


def test_falha_ao_apagar_no_r2_nao_apaga_o_banco(db_session, conta_povoada, capsys):
    conta, linhas = conta_povoada()
    memoria = _FalhaAoApagar()
    _com_original(db_session, conta, memoria, objetos=1)

    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=_Respostas("vitor", conta.cnpj),
        criar_armazenamento=lambda: memoria,
    )

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "Falha ao apagar os arquivos originais no R2 (RuntimeError)." in saida
    assert "Nada foi apagado do banco." in saida
    assert "r2.cloudflarestorage" not in saida
    assert len(_existentes(db_session, linhas)) == len(linhas)
    assert len(memoria.objetos) == 1


def test_falha_ao_consultar_o_r2_nao_apaga_nada(db_session, conta_povoada, capsys):
    conta, linhas = conta_povoada()
    memoria = ArmazenamentoEmMemoria()
    _com_original(db_session, conta, memoria, objetos=1)
    memoria.falhar = True
    perguntar = _Respostas()

    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=perguntar,
        criar_armazenamento=lambda: memoria,
    )

    saida = capsys.readouterr().out
    assert codigo == 1
    assert perguntar.perguntas == []
    assert "RuntimeError" in saida
    assert "r2.cloudflarestorage" not in saida
    assert len(_existentes(db_session, linhas)) == len(linhas)


def test_falha_ao_criar_o_cliente_do_r2_nao_apaga_nada(db_session, conta_povoada, capsys):
    # Revisão final (I1): o cliente do R2 que não sobe não derruba o script com
    # o endpoint no traceback.
    conta, linhas = conta_povoada()

    def _quebrado():
        raise ValueError("Invalid endpoint: abc.r2.cloudflarestorage.com")

    perguntar = _Respostas()
    codigo = script.main(
        ["--conta-id", str(conta.id), "--executar"],
        perguntar=perguntar,
        criar_armazenamento=_quebrado,
    )

    saida = capsys.readouterr().out
    assert codigo == 1
    assert perguntar.perguntas == []
    assert "ValueError" in saida
    assert "abc.r2" not in saida
    assert len(_existentes(db_session, linhas)) == len(linhas)
