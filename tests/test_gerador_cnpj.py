"""O gerador de CNPJ alfanumérico dos testes (tests/conftest.py). A raiz vinha
de uuid4().hex[:8] e às vezes saía só com dígitos, o que derrubava de vez em
quando os testes que contam com a letra (rodadas 2 e 6 do run 39276294020)."""

from conftest import _gerar_cnpj_alfanumerico

from app.core.cnpj import digito_verificador, normalizar

CHAMADAS = 2000


def test_cnpj_alfanumerico_gerado_sempre_tem_letra_e_verificadores_validos():
    cnpjs = [_gerar_cnpj_alfanumerico() for _ in range(CHAMADAS)]

    for cnpj in cnpjs:
        assert len(cnpj) == 14
        assert not cnpj.isdigit()
        assert any(c.isalpha() for c in cnpj[:8])
        assert cnpj[8:12] == "0001"
        dv1 = digito_verificador(cnpj[:12])
        assert cnpj[12:] == dv1 + digito_verificador(cnpj[:12] + dv1)
        assert normalizar(cnpj) == cnpj
