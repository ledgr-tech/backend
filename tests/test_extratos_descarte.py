"""Testes de DELETE /extratos/{id} (issue #185): descartar o extrato que nunca
entrou em conciliação.

Precisam de Postgres real (`db_session` do conftest.py) e pulam sem ele. O R2
é o `ArmazenamentoEmMemoria` da fixture `arquivo_original`.
"""

import logging
import threading
import time
import uuid

import pytest
from conftest import cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models import (
    Conciliacao,
    Conta,
    DecisaoLinha,
    ExecucaoConciliacao,
    Extrato,
    Lancamento,
    LinhaInvalida,
    Usuario,
)
from app.services.descarte_extrato import (
    MENSAGEM_EXTRATO_IMPORTANDO,
    MENSAGEM_EXTRATO_NAO_ENCONTRADO,
    MENSAGEM_EXTRATO_USADO,
)
from app.services.travas import travar_extrato_banco
from main import app

client = TestClient(app)


def _descartar(headers, extrato_id):
    return client.delete(f"/extratos/{extrato_id}", headers=headers)


def _ids_listados(headers) -> list[str]:
    return [item["extrato_id"] for item in client.get("/extratos", headers=headers).json()["itens"]]


def _existe(db_session, extrato_id) -> bool:
    db_session.expire_all()
    return db_session.get(Extrato, extrato_id) is not None


def _contar(db_session, modelo, extrato_id) -> int:
    return db_session.scalar(
        select(func.count()).select_from(modelo).where(modelo.extrato_id == extrato_id)
    )


@pytest.fixture
def par(criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers):
    """Empresa com um extrato do banco e um do sistema, com lançamentos."""
    empresa = criar_empresa()
    banco = criar_extrato_da_empresa(empresa.id, "banco")
    sistema = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(banco, [("10.00", "a"), ("20.00", "b")])
    inserir_lancamentos(sistema, [("10.00", "a"), ("20.00", "b")])
    return empresa, auth_headers(empresa.id), banco, sistema


# --- o caminho feliz ------------------------------------------------------------


def test_descarta_o_extrato_com_lancamentos_e_linhas_nao_lidas(
    db_session, criar_empresa, criar_extrato_da_empresa, inserir_lancamentos, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    alvo = criar_extrato_da_empresa(empresa.id, "sistema", status="concluido_com_erros")
    outro = criar_extrato_da_empresa(empresa.id, "sistema")
    inserir_lancamentos(alvo, [("10.00", "a"), ("20.00", "b")])
    inserir_lancamentos(outro, [("30.00", "c")])
    db_session.add_all(
        [
            LinhaInvalida(extrato_id=alvo.id, identificador="3", motivo="data inválida"),
            LinhaInvalida(extrato_id=outro.id, identificador="1", motivo="valor inválido"),
        ]
    )
    db_session.commit()
    alvo_id, outro_id = alvo.id, outro.id
    assert set(_ids_listados(headers)) == {str(alvo_id), str(outro_id)}

    resposta = _descartar(headers, alvo_id)

    assert resposta.status_code == 204
    assert resposta.content == b""
    assert _ids_listados(headers) == [str(outro_id)]
    assert client.get(f"/extratos/{alvo_id}", headers=headers).status_code == 404
    assert not _existe(db_session, alvo_id)
    assert _contar(db_session, Lancamento, alvo_id) == 0
    assert _contar(db_session, LinhaInvalida, alvo_id) == 0
    # O outro extrato da empresa não é tocado.
    assert _existe(db_session, outro_id)
    assert _contar(db_session, Lancamento, outro_id) == 1
    assert _contar(db_session, LinhaInvalida, outro_id) == 1


@pytest.mark.parametrize("status", ["concluido", "concluido_com_erros", "erro"])
def test_descarta_extrato_nao_conciliado_em_qualquer_status_final(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, status
):
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco", status=status)

    resposta = _descartar(auth_headers(empresa.id), extrato.id)

    assert resposta.status_code == 204
    assert not _existe(db_session, extrato.id)


def test_descartar_duas_vezes_da_404_na_segunda(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    extrato = criar_extrato_da_empresa(empresa.id, "banco")

    assert _descartar(headers, extrato.id).status_code == 204
    segunda = _descartar(headers, extrato.id)

    assert segunda.status_code == 404
    assert segunda.json() == {"detail": MENSAGEM_EXTRATO_NAO_ENCONTRADO}


def test_reenviar_o_mesmo_arquivo_continua_criando_outro_extrato(criar_empresa, auth_headers):
    headers = auth_headers(criar_empresa().id)
    conteudo = b"data,valor,descricao\n2026-09-01,10.00,teste\n"

    def enviar():
        return client.post(
            "/extratos/upload",
            headers=headers,
            data={"origem": "sistema"},
            files={"arquivo": ("extrato.csv", conteudo, "text/csv")},
        )

    primeiro, segundo = enviar(), enviar()

    assert primeiro.json()["extrato_id"] != segundo.json()["extrato_id"]


# --- extrato usado: 409 -----------------------------------------------------------


@pytest.mark.parametrize("lado", ["banco", "sistema"])
def test_extrato_usado_em_execucao_da_409_e_continua_na_listagem(par, lado):
    _, headers, banco, sistema = par
    conciliou = client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )
    assert conciliou.status_code == 201
    alvo = banco if lado == "banco" else sistema

    resposta = _descartar(headers, alvo.id)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": MENSAGEM_EXTRATO_USADO}
    assert MENSAGEM_EXTRATO_USADO == (
        "Este extrato já foi usado em uma conciliação e não pode ser descartado."
    )
    assert str(alvo.id) in _ids_listados(headers)


@pytest.mark.parametrize("lado", ["banco", "sistema"])
def test_extrato_com_conciliacao_sem_execucao_da_409(db_session, par, lado):
    empresa, headers, banco, sistema = par
    lancamento = db_session.scalars(
        select(Lancamento).where(Lancamento.extrato_id == banco.id)
    ).first()
    db_session.add(
        Conciliacao(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            lancamento_banco_id=lancamento.id,
            status="sem_correspondencia",
        )
    )
    db_session.commit()
    alvo = banco if lado == "banco" else sistema

    resposta = _descartar(headers, alvo.id)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": MENSAGEM_EXTRATO_USADO}
    assert _existe(db_session, alvo.id)
    assert _contar(db_session, Lancamento, alvo.id) == 2


@pytest.mark.parametrize("lado", ["banco", "sistema"])
def test_extrato_com_decisao_sem_execucao_da_409(db_session, par, lado):
    empresa, headers, banco, sistema = par
    usuario = db_session.scalars(
        select(Usuario).where(Usuario.conta_id == empresa.conta_id)
    ).first()
    db_session.add(
        DecisaoLinha(
            empresa_id=empresa.id,
            extrato_banco_id=banco.id,
            extrato_sistema_id=sistema.id,
            chave="a" * 64,
            tipo="justificada",
            texto="direto no banco",
            usuario_id=usuario.id,
            autor_nome=usuario.nome,
            rodada=1,
        )
    )
    db_session.commit()
    alvo = banco if lado == "banco" else sistema

    resposta = _descartar(headers, alvo.id)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": MENSAGEM_EXTRATO_USADO}
    assert _existe(db_session, alvo.id)


@pytest.mark.parametrize("status", ["pendente", "processando"])
def test_extrato_ainda_sendo_importado_da_409_com_texto_proprio(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, status
):
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco", status=status)

    resposta = _descartar(auth_headers(empresa.id), extrato.id)

    assert resposta.status_code == 409
    assert resposta.json() == {"detail": MENSAGEM_EXTRATO_IMPORTANDO}
    assert MENSAGEM_EXTRATO_IMPORTANDO != MENSAGEM_EXTRATO_USADO
    assert _existe(db_session, extrato.id)


# --- 404 --------------------------------------------------------------------------


def test_extrato_de_outra_empresa_da_404_e_nao_e_apagado(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    minha, outra = criar_empresa(), criar_empresa()
    alheio = criar_extrato_da_empresa(outra.id, "banco")

    resposta = _descartar(auth_headers(minha.id), alheio.id)

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": MENSAGEM_EXTRATO_NAO_ENCONTRADO}
    assert _existe(db_session, alheio.id)


def test_extrato_de_outra_empresa_da_mesma_conta_da_404(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    primeira = criar_empresa()
    segunda = criar_empresa(conta_id=primeira.conta_id)
    extrato = criar_extrato_da_empresa(segunda.id, "banco")

    resposta = _descartar(auth_headers(primeira.id), extrato.id)

    assert resposta.status_code == 404
    assert _existe(db_session, extrato.id)


def test_extrato_inexistente_da_404_igual_ao_de_outra_empresa(
    criar_empresa, criar_extrato_da_empresa, auth_headers
):
    minha, outra = criar_empresa(), criar_empresa()
    alheio = criar_extrato_da_empresa(outra.id, "banco")
    headers = auth_headers(minha.id)

    do_outro = _descartar(headers, alheio.id)
    inexistente = _descartar(headers, uuid.uuid4())

    assert inexistente.status_code == do_outro.status_code == 404
    assert inexistente.json() == do_outro.json()


def test_rascunho_da_importacao_ia_da_404_como_o_get(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    rascunho = criar_extrato_da_empresa(empresa.id, "sistema", status="rascunho")

    assert client.get(f"/extratos/{rascunho.id}", headers=headers).status_code == 404
    resposta = _descartar(headers, rascunho.id)

    assert resposta.status_code == 404
    assert _existe(db_session, rascunho.id)


def test_id_que_nao_e_uuid_da_422(criar_empresa, auth_headers):
    assert _descartar(auth_headers(criar_empresa().id), "abc").status_code == 422


# --- autenticação e papéis --------------------------------------------------------


def test_sem_token_da_401(criar_empresa, criar_extrato_da_empresa):
    extrato = criar_extrato_da_empresa(criar_empresa().id, "banco")

    assert client.delete(f"/extratos/{extrato.id}").status_code == 401


@pytest.mark.parametrize("papel", ["admin", "gestor", "analista"])
def test_quem_pode_enviar_extrato_pode_descartar(
    db_session,
    criar_empresa,
    criar_extrato_da_empresa,
    criar_usuario,
    gerar_token,
    papel,
):
    empresa = criar_empresa()
    usuario = criar_usuario(conta_id=empresa.conta_id, papel=papel)
    token = gerar_token(empresa.conta_id, usuario_id=usuario.id)
    extrato = criar_extrato_da_empresa(empresa.id, "banco")

    resposta = _descartar(cabecalhos(token, empresa.id), extrato.id)

    assert resposta.status_code == 204
    assert not _existe(db_session, extrato.id)


def test_conta_inativa_da_403_conta_inativa_e_nao_apaga(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    empresa = criar_empresa()
    headers = auth_headers(empresa.id)
    extrato = criar_extrato_da_empresa(empresa.id, "banco")
    db_session.execute(
        update(Conta)
        .where(Conta.id == empresa.conta_id)
        .values(inativada_em=func.now(), motivo_inativacao="teste")
    )
    db_session.commit()

    resposta = _descartar(headers, extrato.id)

    assert resposta.status_code == 403
    assert resposta.json()["codigo"] == "conta_inativa"
    assert _existe(db_session, extrato.id)
    # A leitura continua: a conta inativa só não escreve.
    assert str(extrato.id) in _ids_listados(headers)


# --- o original no R2 (ADR-023) -----------------------------------------------------


def _com_original(db_session, extrato: Extrato, arquivo_original) -> str:
    chave = f"contas/{uuid.uuid4()}/empresas/{extrato.empresa_id}/{extrato.id}"
    arquivo_original.guardar(chave, b"conteudo")
    extrato.arquivo_chave = chave
    db_session.commit()
    return chave


def test_apaga_o_original_no_r2_depois_do_descarte(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, arquivo_original
):
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco")
    chave = _com_original(db_session, extrato, arquivo_original)
    assert chave in arquivo_original.armazenamento.objetos

    resposta = _descartar(auth_headers(empresa.id), extrato.id)

    assert resposta.status_code == 204
    assert chave not in arquivo_original.armazenamento.objetos


def test_falha_no_r2_nao_quebra_o_204_e_o_log_nao_traz_o_endpoint(
    db_session,
    criar_empresa,
    criar_extrato_da_empresa,
    auth_headers,
    arquivo_original,
    caplog,
):
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco")
    extrato.nome_arquivo = "extrato-da-maria-silva.csv"
    chave = _com_original(db_session, extrato, arquivo_original)
    extrato_id = extrato.id
    arquivo_original.armazenamento.falhar = True

    with caplog.at_level(logging.INFO):
        resposta = _descartar(auth_headers(empresa.id), extrato_id)

    assert resposta.status_code == 204
    assert not _existe(db_session, extrato_id)
    # O objeto ficou no R2 (a falha foi só registrada)...
    assert chave in arquivo_original.armazenamento.objetos
    # ...e o log só tem a classe do erro e o id, sem endpoint nem nome de arquivo.
    texto = caplog.text
    assert "RuntimeError" in texto
    assert str(extrato_id) in texto
    assert "cloudflarestorage" not in texto
    assert "maria-silva" not in texto


def test_no_409_o_original_nao_e_apagado(db_session, par, arquivo_original):
    _, headers, banco, sistema = par
    chave = _com_original(db_session, banco, arquivo_original)
    client.post(
        "/conciliacoes",
        headers=headers,
        json={"extrato_banco_id": str(banco.id), "extrato_sistema_id": str(sistema.id)},
    )

    resposta = _descartar(headers, banco.id)

    assert resposta.status_code == 409
    assert chave in arquivo_original.armazenamento.objetos


def test_no_409_de_importando_o_original_nao_e_apagado(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers, arquivo_original
):
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco", status="processando")
    chave = _com_original(db_session, extrato, arquivo_original)

    assert _descartar(auth_headers(empresa.id), extrato.id).status_code == 409
    assert chave in arquivo_original.armazenamento.objetos


def test_sem_r2_configurado_o_descarte_segue(
    db_session, criar_empresa, criar_extrato_da_empresa, auth_headers
):
    # Flag desligada (padrão dos testes): `servico` é None e o extrato tem chave.
    empresa = criar_empresa()
    extrato = criar_extrato_da_empresa(empresa.id, "banco")
    extrato.arquivo_chave = "contas/x/empresas/y/z"
    db_session.commit()

    resposta = _descartar(auth_headers(empresa.id), extrato.id)

    assert resposta.status_code == 204
    assert not _existe(db_session, extrato.id)


# --- concorrência -----------------------------------------------------------------


def _execucao_vazia(empresa_id, banco_id, sistema_id) -> ExecucaoConciliacao:
    return ExecucaoConciliacao(
        empresa_id=empresa_id,
        extrato_banco_id=banco_id,
        extrato_sistema_id=sistema_id,
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


def test_descarte_espera_a_conciliacao_em_andamento_e_depois_da_409(db_session, par):
    """A conciliação pega a trava do extrato do banco e grava a execução; o
    DELETE, que chega no meio, espera o commit dela e então vê a execução."""
    empresa, headers, banco, sistema = par
    banco_id, sistema_id = banco.id, sistema.id
    resultado = {}

    with Session(engine) as conciliacao:
        travar_extrato_banco(conciliacao, empresa.id, banco_id)
        conciliacao.add(_execucao_vazia(empresa.id, banco_id, sistema_id))
        conciliacao.flush()

        def descartar():
            resultado["resposta"] = _descartar(headers, banco_id)

        fio = threading.Thread(target=descartar)
        fio.start()
        time.sleep(0.5)
        # Está esperando a trava: nada foi respondido nem apagado.
        assert fio.is_alive()
        conciliacao.commit()
        fio.join(timeout=10)

    assert not fio.is_alive()
    assert resultado["resposta"].status_code == 409
    assert _existe(db_session, banco_id)
    assert _contar(db_session, Lancamento, banco_id) == 2
