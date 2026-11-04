"""Migração da issue #131 (ADR-027): agrupamento N para 1 e 1 para N.

`conciliacoes` ganha `grupo_id` e os status/regra novos com os CHECKs que os
amarram; `execucoes_conciliacao` ganha `match_agrupado` e `agrupamentos`. O
downgrade devolve ao motor antigo o que ele teria feito (docstring da
migração), com e sem dados agrupados.

Roda em banco descartável (fixture `url_banco_descartavel` do conftest.py),
nunca no Postgres compartilhado de desenvolvimento.
"""

import uuid

import pytest
from conftest import alembic_cli
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REVISAO_ANTERIOR = "e8b3a6d1f294"


@pytest.fixture
def banco(url_banco_descartavel):
    """Banco na revisão anterior com conta, empresa, os dois extratos e quatro
    lançamentos (dois de cada lado)."""
    alembic_cli(url_banco_descartavel, "upgrade", REVISAO_ANTERIOR)
    engine = create_engine(url_banco_descartavel)
    ids = {k: uuid.uuid4() for k in ("conta", "empresa", "banco", "sistema")}
    ids["lancamentos"] = [uuid.uuid4() for _ in range(4)]
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO contas (id, razao_social, cnpj) VALUES (:c, 'C', '12345678000199')"),
            {"c": ids["conta"]},
        )
        conn.execute(
            text(
                "INSERT INTO empresas (id, conta_id, razao_social, cnpj) "
                "VALUES (:e, :c, 'E', '12345678000199')"
            ),
            {"e": ids["empresa"], "c": ids["conta"]},
        )
        for chave in ("banco", "sistema"):
            conn.execute(
                text(
                    "INSERT INTO extratos (id, empresa_id, nome_arquivo, formato, "
                    "tamanho_bytes, status, origem) VALUES (:x, :e, 'a.csv', 'csv', 0, "
                    "'concluido', :o)"
                ),
                {"x": ids[chave], "e": ids["empresa"], "o": chave},
            )
        for indice, lancamento in enumerate(ids["lancamentos"]):
            conn.execute(
                text(
                    "INSERT INTO lancamentos (id, empresa_id, extrato_id, data, valor, "
                    "descricao, tipo, hash_dedup) VALUES (:l, :e, :x, '2026-09-05', 10.00, "
                    "'d', 'debito', :h)"
                ),
                {
                    "l": lancamento,
                    "e": ids["empresa"],
                    "x": ids["banco"] if indice < 2 else ids["sistema"],
                    "h": f"h{indice}",
                },
            )
    yield url_banco_descartavel, engine, ids
    engine.dispose()


def _conciliacao(
    engine, ids, *, status, banco=None, sistema=None, regra=None, score=None, grupo=None
):
    # `grupo_id` só entra no INSERT quando há grupo: antes do upgrade a coluna não existe.
    colunas = "grupo_id, " if grupo is not None else ""
    valores = ":grupo, " if grupo is not None else ""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO conciliacoes (id, empresa_id, extrato_banco_id, extrato_sistema_id, "
                f"lancamento_banco_id, lancamento_sistema_id, status, regra_aplicada, "
                f"score_confianca, {colunas}criado_em) VALUES (gen_random_uuid(), :e, :b, :s, "
                f":lb, :ls, :status, :regra, :score, {valores}now())"
            ),
            {
                "e": ids["empresa"],
                "b": ids["banco"],
                "s": ids["sistema"],
                "lb": banco,
                "ls": sistema,
                "status": status,
                "regra": regra,
                "score": score,
                **({"grupo": grupo} if grupo is not None else {}),
            },
        )


def _execucao(engine, ids, **contagens):
    colunas = {
        "total": 0,
        "match_exato": 0,
        "match_tolerancia": 0,
        "duplicado": 0,
        "sem_correspondencia": 0,
        "tarifa_bancaria": 0,
        "divergente_valor": 0,
        "divergente_data": 0,
        **contagens,
    }
    nomes = ", ".join(colunas)
    valores = ", ".join(f":{nome}" for nome in colunas)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO execucoes_conciliacao (id, empresa_id, extrato_banco_id, "
                f"extrato_sistema_id, tolerancia_dias, {nomes}) VALUES "
                f"(gen_random_uuid(), :e, :b, :s, 0, {valores})"
            ),
            {"e": ids["empresa"], "b": ids["banco"], "s": ids["sistema"], **colunas},
        )


def _colunas(engine, tabela):
    return {coluna["name"] for coluna in inspect(engine).get_columns(tabela)}


def test_antes_do_upgrade_match_agrupado_e_rejeitado(banco):
    _, engine, ids = banco
    with pytest.raises(IntegrityError):
        _conciliacao(
            engine, ids, status="match_agrupado", banco=ids["lancamentos"][0], regra="agrupamento"
        )
    assert "grupo_id" not in _colunas(engine, "conciliacoes")
    assert "match_agrupado" not in _colunas(engine, "execucoes_conciliacao")


def test_upgrade_aceita_match_agrupado_e_amarra_grupo_id_e_ponta(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", "head")
    grupo = uuid.uuid4()
    banco_a, banco_b, sistema_a, sistema_b = ids["lancamentos"]

    _conciliacao(
        engine,
        ids,
        status="match_agrupado",
        banco=banco_a,
        regra="agrupamento",
        score="0.900",
        grupo=grupo,
    )
    _conciliacao(
        engine,
        ids,
        status="match_agrupado",
        sistema=sistema_a,
        regra="agrupamento",
        score="0.900",
        grupo=grupo,
    )
    # grupo_id só na linha match_agrupado, e toda match_agrupado tem grupo_id
    with pytest.raises(IntegrityError):
        _conciliacao(
            engine, ids, status="match_exato", banco=banco_b, sistema=sistema_b, grupo=grupo
        )
    with pytest.raises(IntegrityError):
        _conciliacao(engine, ids, status="match_agrupado", banco=banco_b, regra="agrupamento")
    # exatamente uma ponta: as duas ou nenhuma (esta já é barrada pelo CHECK antigo) não valem
    with pytest.raises(IntegrityError):
        _conciliacao(
            engine,
            ids,
            status="match_agrupado",
            banco=banco_b,
            sistema=sistema_b,
            regra="agrupamento",
            grupo=grupo,
        )
    # as demais linhas seguem como antes
    _conciliacao(engine, ids, status="match_exato", banco=banco_b, sistema=sistema_b, regra="exato")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM conciliacoes")).scalar_one() == 3


def test_upgrade_deixa_as_execucoes_antigas_com_zero_e_soma_inclui_o_agrupado(banco):
    url, engine, ids = banco
    _execucao(engine, ids, total=3, match_exato=2, sem_correspondencia=1)
    alembic_cli(url, "upgrade", "head")

    with engine.connect() as conn:
        antiga = conn.execute(
            text("SELECT match_agrupado, agrupamentos FROM execucoes_conciliacao")
        ).one()
    assert tuple(antiga) == (0, 0)

    # 11 linhas agrupadas de 1 grupo: o total conta as linhas, `agrupamentos` fica fora da soma
    _execucao(engine, ids, total=12, match_exato=1, match_agrupado=11, agrupamentos=1)
    with pytest.raises(IntegrityError):
        _execucao(engine, ids, total=12, match_exato=1, match_agrupado=10, agrupamentos=1)
    with pytest.raises(IntegrityError):
        _execucao(engine, ids, total=1, match_exato=2, match_agrupado=-1)


def test_downgrade_sem_dados_agrupados_volta_ao_esquema_antigo(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", "head")
    _conciliacao(
        engine,
        ids,
        status="match_exato",
        banco=ids["lancamentos"][0],
        sistema=ids["lancamentos"][2],
        regra="exato",
    )
    _execucao(engine, ids, total=1, match_exato=1)

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    assert "grupo_id" not in _colunas(engine, "conciliacoes")
    assert not {"match_agrupado", "agrupamentos"} & _colunas(engine, "execucoes_conciliacao")
    with pytest.raises(IntegrityError):
        _conciliacao(engine, ids, status="match_agrupado", banco=ids["lancamentos"][1])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM conciliacoes")).scalar_one() == "match_exato"
        assert conn.execute(text("SELECT total FROM execucoes_conciliacao")).scalar_one() == 1

    alembic_cli(url, "upgrade", "head")
    assert "grupo_id" in _colunas(engine, "conciliacoes")


def test_downgrade_com_dados_agrupados_devolve_as_linhas_a_divergencia(banco):
    url, engine, ids = banco
    alembic_cli(url, "upgrade", "head")
    grupo = uuid.uuid4()
    banco_a, banco_b, sistema_a, sistema_b = ids["lancamentos"]
    _conciliacao(
        engine,
        ids,
        status="match_exato",
        banco=banco_a,
        sistema=sistema_a,
        regra="exato",
        score="1.000",
    )
    for lancamento in (banco_b,):
        _conciliacao(
            engine,
            ids,
            status="match_agrupado",
            banco=lancamento,
            regra="agrupamento",
            score="0.900",
            grupo=grupo,
        )
    _conciliacao(
        engine,
        ids,
        status="match_agrupado",
        sistema=sistema_b,
        regra="agrupamento",
        score="0.900",
        grupo=grupo,
    )
    _execucao(engine, ids, total=3, match_exato=1, match_agrupado=2, agrupamentos=1)
    _execucao(engine, ids, total=5, sem_correspondencia=2, match_agrupado=3, agrupamentos=1)
    _execucao(engine, ids, total=4, match_exato=4)  # sem agrupamento: não muda

    alembic_cli(url, "downgrade", REVISAO_ANTERIOR)

    with engine.connect() as conn:
        linhas = conn.execute(
            text(
                "SELECT status, regra_aplicada, score_confianca FROM conciliacoes "
                "ORDER BY status"
            )
        ).all()
        execucoes = conn.execute(
            text(
                "SELECT total, match_exato, sem_correspondencia FROM execucoes_conciliacao "
                "ORDER BY total"
            )
        ).all()
    assert [tuple(linha) for linha in linhas] == [
        ("match_exato", "exato", 1),
        ("sem_correspondencia", None, None),
        ("sem_correspondencia", None, None),
    ]
    # o total não muda e a soma antiga continua valendo (o CHECK antigo foi recriado com sucesso)
    assert [tuple(execucao) for execucao in execucoes] == [(3, 1, 2), (4, 4, 0), (5, 0, 5)]

    # o upgrade volta a funcionar depois do downgrade
    alembic_cli(url, "upgrade", "head")
