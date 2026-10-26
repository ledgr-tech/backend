"""Arquivo original do extrato (issue #139, ADR-023): o upload guarda o arquivo
cifrado nas contas do plano avançado, a listagem e o detalhe dizem se há
original, e o admin e o gestor baixam.

O R2 é trocado por um armazenamento em memória (fixture `arquivo_original`,
em tests/conftest.py). O plano da conta é gravado direto no banco.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import CHAVE_CIFRA_TESTE
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.models import Conta, Empresa, Extrato
from app.services.arquivo_original import ArquivoOriginal, obter_arquivo_original
from main import app

client = TestClient(app)

OFX = (Path(__file__).parent / "fixtures" / "extrato_valido.ofx").read_bytes()


EM_TESTE = datetime(2099, 1, 1, tzinfo=UTC)


def _no_plano(db_session, empresa: Empresa, plano: str | None, teste_termina_em=EM_TESTE) -> Conta:
    # O CHECK de `contas` exige o plano e o fim do teste juntos.
    conta = db_session.get(Conta, empresa.conta_id)
    conta.plano = plano
    conta.teste_termina_em = teste_termina_em if plano is not None else None
    db_session.commit()
    return conta


def _enviar(headers, nome: str = "extrato.ofx", conteudo: bytes = OFX, origem: str = "banco"):
    return client.post(
        "/extratos/upload",
        headers=headers,
        data={"origem": origem},
        files={"arquivo": (nome, conteudo, "application/octet-stream")},
    )


def _extrato(db_session, extrato_id) -> Extrato:
    db_session.expire_all()
    return db_session.get(Extrato, uuid.UUID(extrato_id))


# --- upload ------------------------------------------------------------------


@pytest.mark.parametrize("origem", ["banco", "sistema"])
def test_avancado_com_flag_guarda_o_original_cifrado(
    db_session, criar_empresa, auth_headers, arquivo_original, origem
):
    empresa = criar_empresa()
    conta = _no_plano(db_session, empresa, "avancado")

    response = _enviar(auth_headers(empresa.id), origem=origem)

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is True
    extrato_id = response.json()["extrato_id"]
    chave = _extrato(db_session, extrato_id).arquivo_chave
    assert chave == f"contas/{conta.id}/empresas/{empresa.id}/{extrato_id}"
    assert arquivo_original.armazenamento.objetos[chave] != OFX
    assert arquivo_original.abrir(chave) == OFX


def test_flag_desligada_nao_guarda(
    db_session, criar_empresa, auth_headers, arquivo_original, monkeypatch
):
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado")
    monkeypatch.setattr(settings, "arquivo_original_habilitado", False)

    response = _enviar(auth_headers(empresa.id))

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is False
    assert _extrato(db_session, response.json()["extrato_id"]).arquivo_chave is None
    assert arquivo_original.armazenamento.objetos == {}


@pytest.mark.parametrize("plano", [None, "essencial", "padrao"])
def test_outros_planos_nao_guardam(
    db_session, criar_empresa, auth_headers, arquivo_original, plano
):
    empresa = criar_empresa()
    _no_plano(db_session, empresa, plano)

    response = _enviar(auth_headers(empresa.id))

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is False
    assert _extrato(db_session, response.json()["extrato_id"]).arquivo_chave is None
    assert arquivo_original.armazenamento.objetos == {}


def test_avancado_com_teste_encerrado_guarda(
    db_session, criar_empresa, auth_headers, arquivo_original
):
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado", teste_termina_em=datetime(2026, 1, 1, tzinfo=UTC))

    response = _enviar(auth_headers(empresa.id))

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is True
    assert len(arquivo_original.armazenamento.objetos) == 1


def test_flag_ligada_sem_configuracao_nao_guarda(
    db_session, criar_empresa, auth_headers, monkeypatch
):
    # Foco 5 do plano: sem as variáveis do R2, o upload segue normal.
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado")
    monkeypatch.setattr(settings, "arquivo_original_habilitado", True)

    response = _enviar(auth_headers(empresa.id))

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is False
    assert _extrato(db_session, response.json()["extrato_id"]).arquivo_chave is None


def test_falha_no_r2_nao_derruba_o_upload_nem_vaza(
    db_session, criar_empresa, auth_headers, arquivo_original, caplog
):
    # Foco 2 do plano: o R2 fora do ar não impede a conciliação.
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado")
    arquivo_original.armazenamento.falhar = True

    response = _enviar(auth_headers(empresa.id), nome="joao-da-silva.ofx")

    assert response.status_code == 201
    assert response.json()["arquivo_original"] is False
    extrato = _extrato(db_session, response.json()["extrato_id"])
    assert extrato.arquivo_chave is None
    assert extrato.status == "concluido"
    assert "RuntimeError" in caplog.text
    assert "joao" not in caplog.text
    assert "r2.cloudflarestorage" not in caplog.text
    assert OFX.decode()[:40] not in caplog.text


class _ApagaAEmpresaAoEnviar:
    """Armazenamento que, depois de gravar, apaga a empresa por outra conexão:
    o commit do extrato falha pela FK, o caminho do 404 que já existe."""

    def __init__(self, armazenamento, empresa_id: uuid.UUID):
        self.armazenamento = armazenamento
        self.empresa_id = empresa_id

    def enviar(self, chave_objeto: str, dados: bytes) -> None:
        self.armazenamento.enviar(chave_objeto, dados)
        engine = create_engine(settings.database_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM empresas WHERE id = :id"), {"id": self.empresa_id})
        finally:
            engine.dispose()

    def __getattr__(self, nome):
        return getattr(self.armazenamento, nome)


def test_commit_falho_apaga_o_objeto_enviado(
    db_session, criar_empresa, auth_headers, arquivo_original
):
    # Foco 3 do plano: nada fica no R2 sem extrato no banco. A fixture tira a
    # troca da dependency no fim.
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado")
    headers = auth_headers(empresa.id)
    memoria = arquivo_original.armazenamento
    servico = ArquivoOriginal(_ApagaAEmpresaAoEnviar(memoria, empresa.id), CHAVE_CIFRA_TESTE)
    app.dependency_overrides[obter_arquivo_original] = lambda: servico

    response = _enviar(headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Empresa não encontrada."
    assert memoria.objetos == {}


# --- listagem e detalhe -------------------------------------------------------


def _extratos_com_e_sem_original(db_session, empresa: Empresa) -> tuple[Extrato, Extrato]:
    com, sem = (
        Extrato(
            empresa_id=empresa.id,
            nome_arquivo="extrato.ofx",
            formato="ofx",
            tamanho_bytes=1,
            origem="banco",
            status="concluido",
            arquivo_chave=chave,
        )
        for chave in ("contas/x/empresas/y/z", None)
    )
    db_session.add_all([com, sem])
    db_session.commit()
    return com, sem


def test_listagem_e_detalhe_mostram_arquivo_original(
    db_session, criar_empresa, auth_headers, arquivo_original, monkeypatch
):
    empresa = criar_empresa()
    com, sem = _extratos_com_e_sem_original(db_session, empresa)
    headers = auth_headers(empresa.id)

    itens = {i["extrato_id"]: i for i in client.get("/extratos", headers=headers).json()["itens"]}
    assert itens[str(com.id)]["arquivo_original"] is True
    assert itens[str(sem.id)]["arquivo_original"] is False
    assert client.get(f"/extratos/{com.id}", headers=headers).json()["arquivo_original"] is True
    assert client.get(f"/extratos/{sem.id}", headers=headers).json()["arquivo_original"] is False

    monkeypatch.setattr(settings, "arquivo_original_habilitado", False)
    itens = client.get("/extratos", headers=headers).json()["itens"]
    assert [i["arquivo_original"] for i in itens] == [False, False]
    assert client.get(f"/extratos/{com.id}", headers=headers).json()["arquivo_original"] is False
