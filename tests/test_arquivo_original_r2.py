"""Armazenamento do arquivo original no R2 e configuração (issue #139, ADR-023).

Sem rede: o cliente do `boto3` é trocado por um falso que grava as chamadas,
como o `ClienteS3Falso` dos testes do backup. Só o teste dos timeouts cria um
cliente de verdade, o que não acessa a rede.
"""

import base64
import io

import pytest
from botocore.exceptions import ClientError

from app.core.config import settings
from app.services import arquivo_original as modulo
from app.services.arquivo_original import (
    ArmazenamentoR2,
    ArquivoOriginal,
    ArquivoOriginalNaoEncontrado,
    armazenamento_do_ambiente,
    arquivo_original_do_ambiente,
    cliente_r2,
    obter_arquivo_original,
)

BUCKET = "ledgr-extratos"
OBJETO = "contas/c/empresas/e/x"
CHAVE = bytes(range(32))

CONFIG_OK = {
    "arquivo_original_r2_endpoint": "https://abc.r2.cloudflarestorage.com",
    "arquivo_original_r2_bucket": BUCKET,
    "arquivo_original_r2_access_key_id": "id-do-token",
    "arquivo_original_r2_secret_access_key": "segredo-do-token",
    "arquivo_original_chave": base64.b64encode(CHAVE).decode(),
}


def _erro(codigo: str, operacao: str = "GetObject") -> ClientError:
    return ClientError({"Error": {"Code": codigo, "Message": "x"}}, operacao)


class _PaginadorFalso:
    def __init__(self, cliente: "ClienteS3Falso"):
        self.cliente = cliente

    def paginate(self, **kwargs):
        self.cliente.chamadas.append(("list_objects_v2", kwargs))
        chaves = sorted(k for k in self.cliente.objetos if k.startswith(kwargs["Prefix"]))
        if not chaves:
            yield {"KeyCount": 0}
        for inicio in range(0, len(chaves), 1000):
            yield {"Contents": [{"Key": chave} for chave in chaves[inicio : inicio + 1000]]}


class ClienteS3Falso:
    def __init__(self, objetos: dict[str, bytes] | None = None):
        self.objetos = dict(objetos or {})
        self.chamadas: list[tuple[str, dict]] = []
        self.erro_get: ClientError | None = None
        self.erros_delete: list[dict] = []

    def put_object(self, **kwargs):
        self.chamadas.append(("put_object", kwargs))
        self.objetos[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        self.chamadas.append(("get_object", kwargs))
        if self.erro_get is not None:
            raise self.erro_get
        if kwargs["Key"] not in self.objetos:
            raise _erro("NoSuchKey")
        return {"Body": io.BytesIO(self.objetos[kwargs["Key"]])}

    def delete_object(self, **kwargs):
        self.chamadas.append(("delete_object", kwargs))
        self.objetos.pop(kwargs["Key"], None)

    def delete_objects(self, **kwargs):
        self.chamadas.append(("delete_objects", kwargs))
        if self.erros_delete:
            return {"Errors": self.erros_delete}
        for objeto in kwargs["Delete"]["Objects"]:
            self.objetos.pop(objeto["Key"], None)
        return {}

    def get_paginator(self, nome: str):
        assert nome == "list_objects_v2"
        return _PaginadorFalso(self)

    def de(self, operacao: str) -> list[dict]:
        return [kwargs for nome, kwargs in self.chamadas if nome == operacao]


def test_enviar_grava_no_bucket_com_a_chave():
    cliente = ClienteS3Falso()
    ArmazenamentoR2(cliente, BUCKET).enviar(OBJETO, b"dados")
    assert cliente.de("put_object") == [{"Bucket": BUCKET, "Key": OBJETO, "Body": b"dados"}]


@pytest.mark.parametrize("codigo", ["NoSuchKey", "404"])
def test_baixar_objeto_inexistente_vira_nao_encontrado(codigo):
    cliente = ClienteS3Falso()
    cliente.erro_get = _erro(codigo)
    with pytest.raises(ArquivoOriginalNaoEncontrado):
        ArmazenamentoR2(cliente, BUCKET).baixar(OBJETO)


def test_baixar_com_outro_erro_sobe_o_erro():
    cliente = ClienteS3Falso({OBJETO: b"dados"})
    cliente.erro_get = _erro("AccessDenied")
    with pytest.raises(ClientError):
        ArmazenamentoR2(cliente, BUCKET).baixar(OBJETO)


def test_baixar_devolve_os_bytes():
    cliente = ClienteS3Falso({OBJETO: b"dados"})
    assert ArmazenamentoR2(cliente, BUCKET).baixar(OBJETO) == b"dados"
    assert cliente.de("get_object") == [{"Bucket": BUCKET, "Key": OBJETO}]


def test_apagar_apaga_o_objeto():
    cliente = ClienteS3Falso({OBJETO: b"dados"})
    ArmazenamentoR2(cliente, BUCKET).apagar(OBJETO)
    assert cliente.objetos == {}
    assert cliente.de("delete_object") == [{"Bucket": BUCKET, "Key": OBJETO}]


def test_apagar_prefixo_pagina_e_apaga_em_lotes_de_mil():
    prefixo = "contas/a/"
    objetos = {f"{prefixo}empresas/e/{i:05d}": b"x" for i in range(2500)}
    objetos["contas/b/empresas/e/1"] = b"de outra conta"
    cliente = ClienteS3Falso(objetos)

    apagados = ArmazenamentoR2(cliente, BUCKET).apagar_prefixo(prefixo)

    assert apagados == 2500
    lotes = cliente.de("delete_objects")
    assert [len(lote["Delete"]["Objects"]) for lote in lotes] == [1000, 1000, 500]
    assert all(lote["Delete"]["Quiet"] is True and lote["Bucket"] == BUCKET for lote in lotes)
    assert [k["Prefix"] for k in cliente.de("list_objects_v2")] == [prefixo]
    assert list(cliente.objetos) == ["contas/b/empresas/e/1"]


def test_apagar_prefixo_com_erro_do_r2_falha():
    cliente = ClienteS3Falso({"contas/a/1": b"x"})
    cliente.erros_delete = [{"Key": "contas/a/1", "Code": "InternalError"}]
    with pytest.raises(RuntimeError):
        ArmazenamentoR2(cliente, BUCKET).apagar_prefixo("contas/a/")


def test_contar_prefixo():
    cliente = ClienteS3Falso({"contas/a/1": b"x", "contas/a/2": b"x", "contas/a/3": b"x"})
    armazenamento = ArmazenamentoR2(cliente, BUCKET)
    assert armazenamento.contar_prefixo("contas/a/") == 3
    assert armazenamento.contar_prefixo("contas/b/") == 0


def test_arquivo_original_guarda_cifrado_e_abre():
    cliente = ClienteS3Falso()
    servico = ArquivoOriginal(ArmazenamentoR2(cliente, BUCKET), CHAVE)

    servico.guardar(OBJETO, b"conteudo do extrato")

    assert cliente.objetos[OBJETO] != b"conteudo do extrato"
    assert servico.abrir(OBJETO) == b"conteudo do extrato"
    servico.apagar(OBJETO)
    assert cliente.objetos == {}


@pytest.mark.parametrize("vazia", sorted(CONFIG_OK))
def test_do_ambiente_sem_alguma_variavel_e_none(vazia):
    cfg = settings.model_copy(update={**CONFIG_OK, vazia: "  "})
    assert arquivo_original_do_ambiente(cfg) is None


def test_do_ambiente_com_chave_invalida_e_none_mas_o_armazenamento_existe():
    cfg = settings.model_copy(update={**CONFIG_OK, "arquivo_original_chave": "curta"})
    assert arquivo_original_do_ambiente(cfg) is None
    # O script de exclusão só precisa do R2, não da chave de cifra.
    assert isinstance(armazenamento_do_ambiente(cfg), ArmazenamentoR2)


def test_do_ambiente_configurado():
    servico = arquivo_original_do_ambiente(settings.model_copy(update=CONFIG_OK))
    assert isinstance(servico, ArquivoOriginal)
    assert servico.chave_cifra == CHAVE


def test_dependency_reaproveita_a_instancia_e_devolve_none_sem_configuracao(monkeypatch):
    assert obter_arquivo_original() is None
    for nome, valor in CONFIG_OK.items():
        monkeypatch.setattr(settings, nome, valor)
    primeira = obter_arquivo_original()
    assert isinstance(primeira, ArquivoOriginal)
    assert obter_arquivo_original() is primeira


def test_cliente_r2_tem_timeouts_curtos():
    # Um R2 lento não pode prender o upload (foco 2 do plano). O botocore
    # guarda `max_attempts` (novas tentativas) como `total_max_attempts`, que
    # conta a primeira: 2 novas tentativas são 3 no total. No pior caso, uns
    # 75 s, abaixo dos 120 s que o front espera pelo upload.
    config = cliente_r2("https://abc.r2.cloudflarestorage.com", "id", "segredo").meta.config
    assert config.connect_timeout == 5
    assert config.read_timeout == 20
    assert config.retries["total_max_attempts"] == 3


def test_repr_nao_mostra_a_chave():
    servico = ArquivoOriginal(ArmazenamentoR2(ClienteS3Falso(), BUCKET), CHAVE)
    assert repr(CHAVE) not in repr(servico)
    assert modulo.TAMANHO_CHAVE_CIFRA == len(CHAVE)
