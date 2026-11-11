"""Script do lançamento da cobrança: seleção, dry-run, aplicação e idempotência."""

import uuid
from datetime import UTC, datetime

from app.core import auth, plano
from app.core.config import Settings
from app.models import AssinaturaStripe
from scripts import iniciar_teste_das_contas as script


def test_flag_nasce_desligada():
    config = Settings(_env_file=None, database_url="postgresql://localhost/teste")
    assert config.teste_no_cadastro_habilitado is False


def test_script_dry_run_aplicacao_e_idempotencia(db_session, criar_conta, monkeypatch, capsys):
    instante = datetime(2026, 11, 11, tzinfo=UTC)
    monkeypatch.setattr(auth, "_agora", lambda: instante)
    anteriores = script.iniciar_testes(db_session)
    elegivel = criar_conta()
    com_cliente = criar_conta()
    com_cliente.stripe_customer_id = "cus_" + uuid.uuid4().hex
    com_plano = criar_conta()
    com_plano.plano = "padrao"
    com_teste = criar_conta()
    com_teste.teste_termina_em = instante
    com_assinatura = criar_conta()
    db_session.add(
        AssinaturaStripe(
            conta_id=com_assinatura.id,
            stripe_subscription_id="sub_" + uuid.uuid4().hex,
            status="canceled",
            plano="essencial",
            periodo="mensal",
        )
    )
    db_session.commit()
    # Usa a CLI real e o mesmo banco local, sem trocar a regra de elegibilidade.
    assert script.main([]) == 0
    assert f"Dry-run: {anteriores + 2} conta(s)." in capsys.readouterr().out
    db_session.expire_all()
    assert elegivel.teste_termina_em is None
    assert com_cliente.teste_termina_em is None
    assert script.main(["--aplicar"]) == 0
    saida = capsys.readouterr().out
    assert f"Aplicado: {anteriores + 2} conta(s)." in saida
    db_session.expire_all()
    fim = instante + plano.DURACAO_TESTE
    assert elegivel.teste_termina_em == com_cliente.teste_termina_em == fim
    assert com_plano.teste_termina_em is None
    assert com_teste.teste_termina_em == instante
    assert com_assinatura.teste_termina_em is None
    monkeypatch.setattr(auth, "_agora", lambda: fim)
    assert script.main(["--aplicar"]) == 0
    assert "Aplicado: 0 conta(s)." in capsys.readouterr().out
    db_session.expire_all()
    assert elegivel.teste_termina_em == com_cliente.teste_termina_em == fim
    for conta in (elegivel, com_cliente, com_plano, com_teste, com_assinatura):
        assert conta.cnpj not in saida
        assert conta.razao_social not in saida
