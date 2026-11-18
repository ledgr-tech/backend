"""Volume das ferramentas do MCP com Postgres real (issue #225).

O mês vem de scripts/gerar_extratos_sinteticos.py: `_gerar_par` gera o par
banco e sistema (75% de sobreposição), `_serializar` o põe em CSV (o do banco com
a coluna de saldo) e `parse_csv`, o mesmo parser do upload, lê de volta. Os
lançamentos entram em lote direto no banco e o par é conciliado pelo POST de
verdade, como em tests/test_conciliacoes_volume.py.

O que se prova, por ferramenta, num mês de 10 mil lançamentos por lado:

- número fixo de consultas: o mesmo com 10 mil e com 2 mil por lado (a regra
  compartilhada com a `/v1`);
- tempo e tamanho do payload, impressos (`-s`) e com teto folgado, só rede
  contra travamento, como no volume da conciliação (o teto não mede a máquina);
- os índices que o Postgres usa, por `EXPLAIN (ANALYZE)` dos comandos reais
  (impressos).

Marcador `volume`, job próprio da CI. Rodar:
`pytest -m volume -s tests/test_mcp_volume.py`.
"""

import hashlib
import importlib.util
import json
import random
import re
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest
from conftest import cabecalhos
from sqlalchemy import event, insert
from test_mcp_oauth import mcp  # noqa: F401 - fixture usada com usefixtures
from test_mcp_servidor import _com_cliente, cenario  # noqa: F401 - `cenario` é fixture

from app.core.database import engine
from app.models import Extrato, Lancamento
from app.parsers.csv import parse_csv

TETO_SEGURANCA_SEGUNDOS = 10.0
TETO_PAYLOAD_LISTA_BYTES = 150_000
LOTE = 5_000

_caminho = Path(__file__).resolve().parent.parent / "scripts" / "gerar_extratos_sinteticos.py"
_spec = importlib.util.spec_from_file_location("gerar_extratos_sinteticos", _caminho)
gerador = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gerador)


def _mes_sintetico(db, empresa_id, por_lado: int, seed: int) -> uuid.UUID:
    """Concilia, pela API, um mês sintético de `por_lado` lançamentos de cada lado.
    Devolve o id do extrato do banco."""

    rng = random.Random(seed)
    banco, sistema = gerador._gerar_par(por_lado, por_lado * 3 // 4, rng)
    ids = {}
    for lado, lancamentos in (("banco", banco), ("sistema", sistema)):
        conteudo = gerador._serializar(
            "csv", lancamentos, "utf-8", False, com_saldo=lado == "banco"
        )
        lidos = parse_csv(conteudo)
        assert len(lidos.lancamentos) == por_lado and not lidos.erros
        extrato = Extrato(
            empresa_id=empresa_id,
            nome_arquivo=f"sintetico_{lado}.csv",
            formato="csv",
            tamanho_bytes=len(conteudo),
            origem=lado,
            status="concluido",
            quantidade_lancamentos=por_lado,
            periodo_inicio=min(l.data for l in lidos.lancamentos),
            periodo_fim=max(l.data for l in lidos.lancamentos),
            saldo_final=lidos.saldo.valor if lidos.saldo else None,
            saldo_data=lidos.saldo.data if lidos.saldo else None,
        )
        db.add(extrato)
        db.commit()
        ids[lado] = extrato.id
        vistos: Counter = Counter()
        linhas = []
        for l in lidos.lancamentos:
            chave = (l.valor, l.data, l.descricao)
            vistos[chave] += 1
            linhas.append(
                {
                    "empresa_id": empresa_id,
                    "extrato_id": extrato.id,
                    "data": l.data,
                    "valor": l.valor,
                    "descricao": l.descricao,
                    "tipo": l.tipo,
                    "hash_dedup": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    "ocorrencia": vistos[chave],
                }
            )
        for inicio in range(0, len(linhas), LOTE):
            db.execute(insert(Lancamento), linhas[inicio : inicio + LOTE])
        db.commit()
    return ids["banco"], ids["sistema"]


def _conciliar(contexto, empresa_id, banco_id, sistema_id) -> None:
    from fastapi.testclient import TestClient

    from main import app

    resposta = TestClient(app).post(
        "/conciliacoes",
        headers=cabecalhos(contexto.jwt, empresa_id),
        json={"extrato_banco_id": str(banco_id), "extrato_sistema_id": str(sistema_id)},
    )
    assert resposta.status_code == 201, resposta.text


class _Consultas:
    """Os comandos SQL que o app manda ao banco enquanto está ligado."""

    def __init__(self) -> None:
        self.comandos: list[tuple[str, object]] = []
        self.ligado = False

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._ouvir)
        return self

    def __exit__(self, *_):
        event.remove(engine, "before_cursor_execute", self._ouvir)

    def _ouvir(self, conn, cursor, statement, parameters, context, executemany):
        if self.ligado and not statement.lstrip().upper().startswith(
            ("BEGIN", "COMMIT", "ROLLBACK")
        ):
            self.comandos.append((statement, parameters))

    def zerar(self) -> None:
        self.comandos = []


def _medir(token: str, consultas: _Consultas) -> dict[str, dict]:
    """Chama as ferramentas de mês numa conexão só e devolve, por ferramenta, o
    tempo, o tamanho do payload e os comandos SQL."""

    async def _funcao(cliente):
        medidas = {}
        lista = await cliente.call_tool("divergencias_do_mes", {"limite": 5})
        um_id = lista.structured_content["itens"][0]["id"]
        chamadas = {
            "resumo_do_mes": {},
            "visao_do_mes": {},
            "pendencias_do_mes": {},
            "divergencias_do_mes (limite 50)": ({}, "divergencias_do_mes"),
            "divergencias_do_mes (limite 200, por valor)": (
                {"limite": 200, "ordenar_por": "valor", "ordem": "desc"},
                "divergencias_do_mes",
            ),
            "divergencias_do_mes (filtro de valor)": (
                {"valor_minimo": 1000, "limite": 200},
                "divergencias_do_mes",
            ),
            "detalhe_da_divergencia": {"id": um_id},
        }
        for rotulo, definicao in chamadas.items():
            nome, argumentos = (
                (definicao[1], definicao[0])
                if isinstance(definicao, tuple)
                else (rotulo, definicao)
            )
            consultas.zerar()
            consultas.ligado = True
            inicio = time.perf_counter()
            resultado = await cliente.call_tool(nome, argumentos)
            duracao = time.perf_counter() - inicio
            consultas.ligado = False
            assert resultado.is_error is not True, (rotulo, resultado.content)
            texto = " ".join(b.text for b in resultado.content if b.type == "text")
            medidas[rotulo] = {
                "segundos": duracao,
                "bytes": len(json.dumps(resultado.structured_content)) + len(texto),
                "comandos": list(consultas.comandos),
            }
        return medidas

    return _com_cliente(token, _funcao)


def _planos(medidas: dict[str, dict]) -> dict[str, list[str]]:
    """`EXPLAIN (ANALYZE)` dos comandos que leem lançamentos ou conciliações."""
    planos: dict[str, list[str]] = {}
    with engine.connect() as conn:
        for rotulo, medida in medidas.items():
            usados = []
            for comando, parametros in medida["comandos"]:
                if not re.search(
                    r"FROM (lancamentos|conciliacoes)|JOIN (lancamentos|conciliacoes)", comando
                ):
                    continue
                linhas = conn.exec_driver_sql("EXPLAIN (ANALYZE) " + comando, parametros).all()
                plano = "\n".join(l[0] for l in linhas)
                usados += re.findall(
                    r"(?:Index(?: Only)? Scan|Bitmap Index Scan) (?:using|on) (\S+)", plano
                )
                usados += [f"Seq Scan on {t}" for t in re.findall(r"Seq Scan on (\w+)", plano)]
            planos[rotulo] = sorted(set(usados))
    return planos


@pytest.mark.volume
@pytest.mark.usefixtures("mcp")
def test_ferramentas_com_10_mil_lancamentos_por_lado(
    cenario,  # noqa: F811
    db_session,
    criar_empresa,
):
    conta_id = cenario.admin.conta_id
    pequena = criar_empresa(razao_social="Pequena Ltda", conta_id=conta_id).id
    grande = criar_empresa(razao_social="Grande Ltda", conta_id=conta_id).id
    _conciliar(cenario, pequena, *_mes_sintetico(db_session, pequena, 2_000, seed=7))
    _conciliar(cenario, grande, *_mes_sintetico(db_session, grande, 10_000, seed=11))

    with _Consultas() as consultas:
        da_pequena = _medir(cenario.conectar(empresa_id=pequena), consultas)
        da_grande = _medir(cenario.conectar(empresa_id=grande), consultas)
    planos = _planos(da_grande)

    print("\nMCP_VOLUME 10 mil lançamentos por lado (7,5 mil conciliados, 5 mil divergências)")
    for rotulo, medida in da_grande.items():
        print(
            f"MCP_VOLUME {rotulo}: {medida['segundos'] * 1000:.0f} ms, "
            f"{medida['bytes'] / 1024:.1f} KB, {len(medida['comandos'])} comandos SQL "
            f"({len(da_pequena[rotulo]['comandos'])} com 2 mil por lado); "
            f"usa {', '.join(planos[rotulo]) or '-'}"
        )

    for rotulo, medida in da_grande.items():
        assert medida["segundos"] < TETO_SEGURANCA_SEGUNDOS, rotulo
        assert medida["bytes"] < TETO_PAYLOAD_LISTA_BYTES, rotulo
        # Número fixo de consultas, qualquer que seja o tamanho do mês.
        assert len(medida["comandos"]) == len(da_pequena[rotulo]["comandos"]), rotulo
