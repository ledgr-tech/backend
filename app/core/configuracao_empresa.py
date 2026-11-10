"""Regras de produto da configuração da empresa, num módulo neutro para as
rotas (app/api/empresa.py) e os serviços (app/services/ia/detalhes.py) lerem
o mesmo valor sem que um serviço importe de app/api."""

# Decisão de Eduardo em 05/10: a tolerância de dias vai de 0 a 5. O limite é
# regra de produto e fica só aqui (o banco garante apenas o mínimo).
TOLERANCIA_DIAS_MAXIMA = 5
