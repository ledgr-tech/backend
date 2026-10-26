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
from conftest import CHAVE_CIFRA_TESTE, cabecalhos
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.extratos import content_disposition
from app.core.config import settings
from app.core.rate_limit import LIMITE_DOWNLOAD_ORIGINAL
from app.models import Conta, Empresa, Extrato
from app.services.arquivo_original import (
    ArquivoOriginal,
    chave_do_objeto,
    obter_arquivo_original,
)
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


# --- download -----------------------------------------------------------------


def _com_original(
    db_session,
    empresa: Empresa,
    servico: ArquivoOriginal,
    conteudo: bytes = OFX,
    nome: str = "extrato.ofx",
    formato: str = "ofx",
) -> Extrato:
    extrato = Extrato(
        id=uuid.uuid4(),
        empresa_id=empresa.id,
        nome_arquivo=nome,
        formato=formato,
        tamanho_bytes=len(conteudo),
        origem="banco",
        status="concluido",
    )
    extrato.arquivo_chave = chave_do_objeto(empresa.conta_id, empresa.id, extrato.id)
    servico.guardar(extrato.arquivo_chave, conteudo)
    db_session.add(extrato)
    db_session.commit()
    return extrato


@pytest.fixture
def como(criar_usuario, gerar_token):
    """Cabeçalhos de um usuário novo da conta da empresa, com o papel dado."""

    def _como(papel: str, empresa: Empresa) -> dict[str, str]:
        usuario = criar_usuario(conta_id=empresa.conta_id, papel=papel)
        return cabecalhos(gerar_token(empresa.conta_id, usuario_id=usuario.id), empresa.id)

    return _como


def _baixar(headers, extrato_id):
    return client.get(f"/extratos/{extrato_id}/arquivo-original", headers=headers)


@pytest.mark.parametrize("papel", ["admin", "gestor"])
def test_admin_e_gestor_baixam_o_original(db_session, criar_empresa, como, arquivo_original, papel):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)

    response = _baixar(como(papel, empresa), extrato.id)

    assert response.status_code == 200
    assert response.content == OFX
    assert response.headers["content-type"].startswith("application/x-ofx")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"


def test_csv_sai_como_text_csv(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()
    extrato = _com_original(
        db_session, empresa, arquivo_original, b"data;valor\n", "extrato.csv", "csv"
    )

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 200
    assert response.content == b"data;valor\n"
    assert response.headers["content-type"].startswith("text/csv")


def test_analista_recebe_403(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)

    response = _baixar(como("analista", empresa), extrato.id)

    assert response.status_code == 403
    assert response.json()["detail"] == "Só o admin ou o gestor da conta podem fazer isso."


def test_flag_desligada_da_404(db_session, criar_empresa, como, arquivo_original, monkeypatch):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)
    monkeypatch.setattr(settings, "arquivo_original_habilitado", False)

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Arquivo original não disponível."


def test_flag_ligada_sem_configuracao_da_503(db_session, criar_empresa, como, monkeypatch):
    # Foco 5 do plano.
    empresa = criar_empresa()
    extrato = Extrato(
        empresa_id=empresa.id,
        nome_arquivo="extrato.ofx",
        formato="ofx",
        tamanho_bytes=1,
        origem="banco",
        status="concluido",
        arquivo_chave="contas/x/empresas/y/z",
    )
    db_session.add(extrato)
    db_session.commit()
    monkeypatch.setattr(settings, "arquivo_original_habilitado", True)

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 503
    assert response.json()["detail"] == "Arquivo original indisponível no momento."


def test_extrato_sem_original_da_404(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()
    _, sem = _extratos_com_e_sem_original(db_session, empresa)

    response = _baixar(como("gestor", empresa), sem.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Arquivo original não disponível."


def test_extrato_inexistente_da_404(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()

    response = _baixar(como("gestor", empresa), uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["detail"] == "Extrato não encontrado."


def test_objeto_que_sumiu_do_bucket_da_404(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)
    arquivo_original.armazenamento.objetos.clear()

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Arquivo original não disponível."


def test_extrato_de_outra_empresa_da_mesma_conta_da_404(
    db_session, criar_empresa, como, arquivo_original
):
    # Foco 4 do plano: a empresa ativa é a do cabeçalho.
    empresa = criar_empresa()
    outra = criar_empresa("Outra cliente", conta_id=empresa.conta_id)
    extrato = _com_original(db_session, outra, arquivo_original)

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Extrato não encontrado."
    assert OFX not in response.content


def test_chave_trocada_da_500_sem_vazar(db_session, criar_empresa, como, arquivo_original, caplog):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)
    outra_chave = ArquivoOriginal(arquivo_original.armazenamento, bytes(32 * [9]))
    app.dependency_overrides[obter_arquivo_original] = lambda: outra_chave

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 500
    assert response.json()["detail"] == "Não foi possível abrir o arquivo original."
    assert "ArquivoOriginalCorrompido" in caplog.text


def test_r2_fora_do_ar_da_503(db_session, criar_empresa, como, arquivo_original, caplog):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)
    arquivo_original.armazenamento.falhar = True

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 503
    assert response.json()["detail"] == "Arquivo original indisponível no momento."
    assert "RuntimeError" in caplog.text
    assert "r2.cloudflarestorage" not in caplog.text


def test_download_continua_depois_de_sair_do_avancado(
    db_session, criar_empresa, como, arquivo_original
):
    empresa = criar_empresa()
    _no_plano(db_session, empresa, "avancado")
    extrato = _com_original(db_session, empresa, arquivo_original)
    _no_plano(db_session, empresa, "padrao")

    response = _baixar(como("gestor", empresa), extrato.id)

    assert response.status_code == 200
    assert response.content == OFX


def test_download_tem_limite_por_conta(db_session, criar_empresa, como, arquivo_original):
    empresa = criar_empresa()
    extrato = _com_original(db_session, empresa, arquivo_original)
    headers = como("gestor", empresa)
    limite = int(LIMITE_DOWNLOAD_ORIGINAL.split("/")[0])

    respostas = [_baixar(headers, extrato.id) for _ in range(limite + 1)]

    assert respostas[-2].status_code == 200
    assert respostas[-1].status_code == 429


def test_content_disposition_com_acento_aspas_e_quebra_de_linha():
    # Foco 1 do plano: o nome vem do usuário e não pode quebrar o cabeçalho.
    valor = content_disposition('extrato "março"\r\nX.ofx')
    assert "\r" not in valor and "\n" not in valor
    assert valor == (
        'attachment; filename="extrato _mar_o___X.ofx"; '
        "filename*=UTF-8''extrato%20%22mar%C3%A7o%22%0D%0AX.ofx"
    )
