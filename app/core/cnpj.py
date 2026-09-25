"""Validação de CNPJ, numérico e alfanumérico (IN RFB 2.229/2024, desde 07/2026)."""

import re

_FORMATO = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
_PESOS = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]


def digito_verificador(base: str) -> str:
    # ord(c) - 48 dá o valor da Receita tanto pra dígito (0-9) quanto pra letra (A=17).
    pesos = _PESOS[-len(base) :]
    resto = sum((ord(c) - 48) * p for c, p in zip(base, pesos, strict=True)) % 11
    return "0" if resto < 2 else str(11 - resto)


def normalizar(valor: str) -> str | None:
    """Devolve o CNPJ sem máscara e em maiúsculas, ou None se for inválido."""
    cnpj = re.sub(r"[.\-/\s]", "", valor).upper()
    if not _FORMATO.fullmatch(cnpj) or len(set(cnpj)) == 1:
        return None
    dv1 = digito_verificador(cnpj[:12])
    if cnpj[12:] != dv1 + digito_verificador(cnpj[:12] + dv1):
        return None
    return cnpj
