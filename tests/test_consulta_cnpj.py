"""Consulta de CNPJ na Receita pela BrasilAPI (issue #119): mapeamento das
respostas, timeout total, cache e fábrica. A API é sempre um
`httpx.MockTransport`; nada sai para a internet. As rotas de cadastro têm
testes próprios em tests/test_consulta_cnpj_cadastro.py.
"""

import logging
import time

import httpx
import pytest

from app.core.config import Settings
from app.services import consulta_cnpj
from app.services.consulta_cnpj import (
    ConsultaBrasilAPI,
    ResultadoConsultaCNPJ,
    consultar_cnpj,
    obter_consulta_cnpj,
)
from app.services.consulta_cnpj import cache as modulo_cache
from app.services.consulta_cnpj.cache import VALIDADE_SEGUNDOS, CacheConsultaCNPJ

CNPJ = "11222333000181"
BASE = "https://brasilapi.test/api/cnpj/v1"
RAZAO = "EMPRESA DE TESTE LTDA"


def corpo_brasilapi(situacao="ATIVA", razao=RAZAO, cnpj=CNPJ) -> dict:
    """O que importa do 200 da BrasilAPI (o corpo real traz bem mais campos)."""
    return {
        "cnpj": cnpj,
        "razao_social": razao,
        "situacao_cadastral": 2 if situacao == "ATIVA" else 8,
        "descricao_situacao_cadastral": situacao,
        "qsa": [{"nome_socio": "NAO DEVE SER LIDO"}],
    }


NAO_ENCONTRADO_BRASILAPI = {
    "message": "CNPJ 11.222.333/0001-81 não encontrado.",
    "type": "not_found",
    "name": "NotFoundError",
}


def _consulta(handler, **kwargs) -> tuple[ConsultaBrasilAPI, list[httpx.Request]]:
    pedidos: list[httpx.Request] = []

    def _registrar(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return handler(request)

    cliente = httpx.Client(transport=httpx.MockTransport(_registrar))
    return ConsultaBrasilAPI(base_url=BASE, cliente_http=cliente, **kwargs), pedidos


def test_ativa_devolve_razao_social_e_chama_a_url_certa():
    consulta, pedidos = _consulta(lambda r: httpx.Response(200, json=corpo_brasilapi()))

    assert consulta.consultar(CNPJ) == ResultadoConsultaCNPJ("ativa", RAZAO)
    assert len(pedidos) == 1
    assert str(pedidos[0].url) == f"{BASE}/{CNPJ}"
    assert pedidos[0].method == "GET"
    assert "authorization" not in pedidos[0].headers


@pytest.mark.parametrize("situacao", ["BAIXADA", "INAPTA", "SUSPENSA", "NULA", "OUTRA"])
def test_qualquer_situacao_diferente_de_ativa_e_inativa(situacao):
    consulta, _ = _consulta(lambda r: httpx.Response(200, json=corpo_brasilapi(situacao)))

    assert consulta.consultar(CNPJ) == ResultadoConsultaCNPJ("inativa", RAZAO)


def test_situacao_e_razao_social_sao_aparadas_e_situacao_ignora_caixa():
    corpo = corpo_brasilapi(situacao=" ativa ", razao="  EMPRESA  ")
    consulta, _ = _consulta(lambda r: httpx.Response(200, json=corpo))

    assert consulta.consultar(CNPJ) == ResultadoConsultaCNPJ("ativa", "EMPRESA")


def test_cnpj_devolvido_com_mascara_e_aceito():
    corpo = corpo_brasilapi(cnpj="11.222.333/0001-81")
    consulta, _ = _consulta(lambda r: httpx.Response(200, json=corpo))

    assert consulta.consultar(CNPJ).situacao == "ativa"


def test_404_da_brasilapi_e_nao_encontrado():
    consulta, _ = _consulta(lambda r: httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI))

    assert consulta.consultar(CNPJ) == ResultadoConsultaCNPJ("nao_encontrado")


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(404, text="<html>Not Found</html>"),
        httpx.Response(404, json={"error": "rota inexistente"}),
        httpx.Response(
            400, json={"message": "CNPJ deve conter 14 dígitos.", "type": "bad_request"}
        ),
        httpx.Response(429, json={"message": "Too many requests"}),
        httpx.Response(500, text="erro"),
        httpx.Response(503, json={"type": "service_unavailable"}),
        httpx.Response(301, headers={"location": "https://outro.test"}),
        httpx.Response(200, text="não é json"),
        httpx.Response(200, json=[corpo_brasilapi()]),
        httpx.Response(200, json={"cnpj": CNPJ}),
        httpx.Response(200, json=corpo_brasilapi(razao="   ")),
        httpx.Response(200, json=corpo_brasilapi(situacao=None)),
        httpx.Response(200, json={**corpo_brasilapi(), "razao_social": 123}),
        httpx.Response(200, json=corpo_brasilapi(cnpj="33000167000101")),
        httpx.Response(200, json={**corpo_brasilapi(), "cnpj": 11222333000181}),
    ],
    ids=[
        "404_sem_corpo_da_brasilapi",
        "404_com_outro_corpo",
        "400_formato",
        "429",
        "500",
        "503",
        "redirecionamento",
        "corpo_nao_json",
        "corpo_lista",
        "sem_razao_nem_situacao",
        "razao_vazia",
        "situacao_nula",
        "razao_nao_texto",
        "outro_cnpj",
        "cnpj_nao_texto",
    ],
)
def test_respostas_fora_do_esperado_sao_indisponivel(resposta):
    consulta, _ = _consulta(lambda r: resposta)

    assert consulta.consultar(CNPJ) == ResultadoConsultaCNPJ("indisponivel")


@pytest.mark.parametrize(
    "excecao", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError]
)
def test_timeout_e_erro_de_rede_sao_indisponivel(excecao):
    def handler(request):
        raise excecao("falhou", request=request)

    consulta, _ = _consulta(handler)

    assert consulta.consultar(CNPJ).situacao == "indisponivel"


def test_url_invalida_e_indisponivel():
    consulta = ConsultaBrasilAPI(base_url="nao é url://", timeout_segundos=1)

    assert consulta.consultar(CNPJ).situacao == "indisponivel"


def test_timeout_e_total_mesmo_com_a_api_lenta():
    def lenta(request):
        time.sleep(0.6)
        return httpx.Response(200, json=corpo_brasilapi())

    consulta, _ = _consulta(lenta, timeout_segundos=0.1)

    inicio = time.monotonic()
    resultado = consulta.consultar(CNPJ)

    assert resultado.situacao == "indisponivel"
    assert time.monotonic() - inicio < 0.5


def test_chave_preenchida_vai_no_authorization():
    consulta, pedidos = _consulta(
        lambda r: httpx.Response(200, json=corpo_brasilapi()), chave="segredo"
    )

    consulta.consultar(CNPJ)

    assert pedidos[0].headers["authorization"] == "Bearer segredo"


def test_cnpj_alfanumerico_vai_no_caminho_em_maiusculas():
    alfanumerico = "12ABC34501DE35"
    consulta, pedidos = _consulta(
        lambda r: httpx.Response(200, json=corpo_brasilapi(cnpj="12.ABC.345/01DE-35"))
    )

    assert consulta.consultar(alfanumerico).situacao == "ativa"
    assert pedidos[0].url.path.endswith("/12ABC34501DE35")


def test_log_nao_traz_cnpj_nem_razao_social(caplog):
    caplog.set_level(logging.INFO, logger="app")
    respostas = iter([httpx.Response(200, json=corpo_brasilapi()), httpx.Response(503, text=RAZAO)])
    consulta, _ = _consulta(lambda r: next(respostas))

    consultar_cnpj(CNPJ, consulta)
    modulo_cache.cache_por_cnpj.limpar()
    consultar_cnpj(CNPJ, consulta)

    texto = caplog.text
    assert "resultado=ativa origem=api" in texto
    assert "resultado=indisponivel origem=api" in texto
    assert "motivo=http status_http=503" in texto
    assert CNPJ not in texto and "11.222.333" not in texto
    assert RAZAO not in texto


# --- cache e consultar_cnpj -----------------------------------------------------


def test_consultar_cnpj_usa_o_cache_por_24_horas():
    consulta, pedidos = _consulta(lambda r: httpx.Response(200, json=corpo_brasilapi()))

    primeiro = consultar_cnpj(CNPJ, consulta)
    segundo = consultar_cnpj(CNPJ, consulta)

    assert primeiro == segundo == ResultadoConsultaCNPJ("ativa", RAZAO)
    assert len(pedidos) == 1


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(200, json=corpo_brasilapi("BAIXADA")),
        httpx.Response(404, json=NAO_ENCONTRADO_BRASILAPI),
    ],
    ids=["inativa", "nao_encontrado"],
)
def test_inativa_e_nao_encontrado_tambem_ficam_em_cache(resposta):
    consulta, pedidos = _consulta(lambda r: resposta)

    consultar_cnpj(CNPJ, consulta)
    consultar_cnpj(CNPJ, consulta)

    assert len(pedidos) == 1


def test_indisponivel_nao_fica_em_cache():
    respostas = iter(
        [httpx.Response(503, text="fora"), httpx.Response(200, json=corpo_brasilapi())]
    )
    consulta, pedidos = _consulta(lambda r: next(respostas))

    assert consultar_cnpj(CNPJ, consulta).situacao == "indisponivel"
    assert consultar_cnpj(CNPJ, consulta).situacao == "ativa"
    assert len(pedidos) == 2


def test_consulta_desligada_nao_consulta_e_e_indisponivel():
    assert consultar_cnpj(CNPJ, None) == ResultadoConsultaCNPJ("indisponivel")


def test_cache_vence_em_24_horas():
    agora = [1000.0]
    cache = CacheConsultaCNPJ(relogio=lambda: agora[0])
    cache.guardar(CNPJ, ResultadoConsultaCNPJ("ativa", RAZAO))

    agora[0] += VALIDADE_SEGUNDOS - 1
    assert cache.obter(CNPJ) == ResultadoConsultaCNPJ("ativa", RAZAO)
    agora[0] += 1
    assert cache.obter(CNPJ) is None


def test_cache_cheio_tira_o_mais_antigo(monkeypatch):
    monkeypatch.setattr(modulo_cache, "MAXIMO_ENTRADAS", 2)
    cache = CacheConsultaCNPJ()
    for cnpj in ("A", "B", "C"):
        cache.guardar(cnpj, ResultadoConsultaCNPJ("nao_encontrado"))

    assert cache.obter("A") is None
    assert cache.obter("B") is not None and cache.obter("C") is not None


# --- fábrica --------------------------------------------------------------------


def _settings(**valores) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://x/y", **valores)


def test_desligada_por_padrao():
    assert Settings.model_fields["cnpj_consulta_habilitada"].default is False
    assert obter_consulta_cnpj(_settings()) is None


def test_ligada_sem_url_nao_consulta():
    assert (
        obter_consulta_cnpj(_settings(cnpj_consulta_habilitada=True, cnpj_consulta_url=" ")) is None
    )


def test_ligada_usa_a_brasilapi_por_padrao():
    consulta = obter_consulta_cnpj(_settings(cnpj_consulta_habilitada=True))

    assert isinstance(consulta, ConsultaBrasilAPI)
    assert consulta._base_url == "https://brasilapi.com.br/api/cnpj/v1"
    assert consulta._timeout_segundos == consulta_cnpj.brasilapi.TIMEOUT_TOTAL_SEGUNDOS == 5.0
