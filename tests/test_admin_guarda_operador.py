"""Toda rota de `/admin` precisa de `Depends(obter_operador)` (issue #198, ADR-025).

O front do painel não confere quem chama, de propósito: quem barra é
`obter_operador`. Uma rota nova de `/admin` sem a dependência ficaria aberta a
qualquer pessoa logada, e os testes de 403 (tests/test_admin.py e
tests/test_admin_assinaturas.py) só cobrem as rotas que já existem. Este teste
é estrutural: olha as rotas montadas no `app`, sem banco e sem requisição.

As rotas são lidas por `fastapi.routing.iter_route_contexts`, o mesmo caminho
que o FastAPI usa para montar o OpenAPI: desde que o `include_router` deixou de
copiar as rotas para `app.routes`, é ele que entrega cada rota com o caminho
completo e com as dependencies do roteador e do `include_router` já somadas.
"""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.applications import Starlette

from app.api import admin, admin_assinaturas
from app.api.admin import obter_operador
from main import app

O_QUE_FAZER = (
    "Toda rota de /admin precisa de Depends(obter_operador) (app/api/admin.py), "
    "direto, por uma dependency que dependa dele ou em dependencies=[...] da rota, "
    "do roteador ou do include_router. Sem isso a rota fica aberta a qualquer "
    "pessoa logada (ADR-025)."
)


def _sob_admin(caminho: str) -> bool:
    return caminho == "/admin" or caminho.startswith("/admin/")


def _caminho(rota) -> str:
    # A rota que não é APIRoute (Mount, Route) e vem de um include_router só
    # traz o caminho com o prefixo em `starlette_route`.
    montada = getattr(rota, "starlette_route", None)
    return getattr(montada, "path", None) or rota.path or ""


def _rotas_admin(aplicacao: FastAPI) -> list:
    return [rota for rota in iter_route_contexts(aplicacao.routes) if _sob_admin(_caminho(rota))]


def _depende_de_operador(dependant) -> bool:
    return any(
        dependencia.call is obter_operador or _depende_de_operador(dependencia)
        for dependencia in dependant.dependencies
    )


def _descricoes(rota) -> list[str]:
    return [f"{metodo} {_caminho(rota)}" for metodo in sorted(rota.methods or [])]


def rotas_admin_sem_operador(aplicacao: FastAPI) -> list[str]:
    """As rotas sob `/admin` sem `obter_operador` em nenhum nível das dependencies.

    A rota sob `/admin` que não é `APIRoute` (um `Mount`, por exemplo) não tem
    dependencies para conferir: entra na lista do mesmo jeito, com o motivo, em
    vez de passar calada.
    """
    faltando: list[str] = []
    for rota in _rotas_admin(aplicacao):
        if not isinstance(rota.original_route, APIRoute):
            tipo = type(rota.original_route).__name__
            faltando.append(
                f"{tipo} {_caminho(rota)} (não é APIRoute: a guarda não pode ser conferida)"
            )
        elif not _depende_de_operador(rota.dependant):
            faltando.extend(_descricoes(rota))
    return faltando


def test_toda_rota_de_admin_depende_de_obter_operador():
    faltando = rotas_admin_sem_operador(app)

    assert faltando == [], (
        "Rotas de /admin sem obter_operador:\n  " + "\n  ".join(faltando) + "\n" + O_QUE_FAZER
    )


def test_as_rotas_dos_roteadores_de_admin_estao_entre_as_conferidas():
    # Sem isto, o teste acima passaria vazio se a busca deixasse de achar as
    # rotas (roteador fora do app, mudança no FastAPI).
    encontradas = {descricao for rota in _rotas_admin(app) for descricao in _descricoes(rota)}
    declaradas = {
        f"{metodo} {rota.path}"
        for roteador in (admin.router, admin_assinaturas.router)
        for rota in roteador.routes
        for metodo in rota.methods
    }

    assert len(declaradas) > 0
    assert declaradas - encontradas == set()


# --- prova de que o helper acusa a rota sem a guarda ------------------------------------


def _por_intermediaria(operador=Depends(obter_operador)):  # noqa: B008
    return operador


def _app_de_exemplo() -> FastAPI:
    roteador = APIRouter(prefix="/admin")

    @roteador.get("/sem-guarda")
    def sem_guarda(): ...

    @roteador.get("/direta")
    def direta(_operador=Depends(obter_operador)): ...  # noqa: B008

    @roteador.post("/aninhada")
    def aninhada(_operador=Depends(_por_intermediaria)): ...  # noqa: B008

    @roteador.get("/no-decorator", dependencies=[Depends(obter_operador)])
    def no_decorator(): ...

    exemplo = FastAPI()
    exemplo.include_router(roteador)
    return exemplo


def test_helper_aponta_so_a_rota_sem_a_guarda():
    assert rotas_admin_sem_operador(_app_de_exemplo()) == ["GET /admin/sem-guarda"]


def test_helper_ve_rota_direto_no_app_e_roteador_incluido_com_prefixo():
    exemplo = _app_de_exemplo()

    @exemplo.get("/admin")
    def raiz(): ...

    @exemplo.get("/administracao")
    def fora_do_admin(): ...

    terceiro = APIRouter()

    @terceiro.delete("/coisa")
    def coisa(): ...

    guardado = APIRouter()

    @guardado.get("/guardada")
    def guardada(): ...

    exemplo.include_router(terceiro, prefix="/admin")
    exemplo.include_router(guardado, prefix="/admin", dependencies=[Depends(obter_operador)])

    assert rotas_admin_sem_operador(exemplo) == [
        "GET /admin/sem-guarda",
        "GET /admin",
        "DELETE /admin/coisa",
    ]


def test_helper_acusa_rota_de_admin_que_nao_pode_ser_conferida():
    exemplo = FastAPI()
    exemplo.mount("/admin/estatico", Starlette())
    # add_route e mount não levam o prefixo do APIRouter, só o do include_router.
    roteador = APIRouter()
    roteador.add_route("/crua", lambda request: None)
    roteador.mount("/montada", Starlette())
    exemplo.include_router(roteador, prefix="/admin")

    faltando = rotas_admin_sem_operador(exemplo)

    assert [descricao.split(" (")[0] for descricao in faltando] == [
        "Mount /admin/estatico",
        "Route /admin/crua",
        "Mount /admin/montada",
    ]
    assert all("não pode ser conferida" in descricao for descricao in faltando)
