<!--
Prompt da importação via IA (issue #158, ADR-013). Servido por
GET /importacoes-ia/prompt. versao_schema é a do JSON que o prompt pede
(schema_v1.json); versao_prompt muda a cada mudança do texto, mesmo sem mudar
o schema. Mudou o texto: suba versao_prompt. Mudou o schema: arquivo de
schema novo e versao_schema nova.

versao_schema: 1
versao_prompt: 1
-->
Você vai converter o relatório de lançamentos de um sistema de gestão, que está no PDF anexado, para o formato de importação do Ledgr.

Responda somente com o JSON, sem nenhum texto antes ou depois, neste formato:

{"versao": "1", "lancamentos": [{"data": "2026-09-05", "valor": "-1234.56", "descricao": "Pagamento fornecedor"}]}

Regras:

1. Crie um item em "lancamentos" para cada lançamento do relatório, de todas as páginas, na mesma ordem do relatório.
2. Inclua todos os lançamentos. Não resuma, não abrevie e não use reticências para encurtar a resposta.
3. "data": a data do lançamento no formato AAAA-MM-DD. Se o relatório mostra 05/09/2026, escreva "2026-09-05". Se o ano tem dois dígitos, escreva o ano completo.
4. "valor": um texto entre aspas, com ponto como separador decimal, sem separador de milhar, sem símbolo de moeda e com no máximo duas casas decimais. Entradas (recebimentos, créditos) são positivas. Saídas (pagamentos, débitos) são negativas, com o sinal de menos na frente.
   - Se o relatório separa entrada e saída em colunas, ou marca com C e D, use essa informação para o sinal. Exemplo: "R$ 1.234,56 D" vira "-1234.56", e "1.234,56 C" vira "1234.56".
   - Valor entre parênteses, como (1.234,56), é saída: escreva "-1234.56".
   - Sinal de menos depois do número, como 1.234,56-, também é saída: escreva "-1234.56".
5. "descricao": o histórico do lançamento como está no relatório. Se ele ocupa mais de uma linha, junte numa linha só.
6. Não inclua como lançamento: saldo anterior, saldo do dia, saldo final, subtotal, total, cabeçalho, rodapé ou número de página.
7. Não invente, não complete e não corrija lançamentos. Copie data, valor e histórico como estão. Se não conseguir ler um lançamento com certeza, deixe-o de fora: a importação será conferida no Ledgr antes de entrar.
8. Use só os campos "versao", "lancamentos", "data", "valor" e "descricao". "versao" é sempre "1".
