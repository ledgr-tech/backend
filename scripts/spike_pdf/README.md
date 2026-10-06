# Spike: leitura de PDF do sistema de gestão (issue #84)

Ferramenta para medir, com amostras reais, quanto a extração de lançamentos de
um PDF de relatório do ERP acerta. Fica fora da aplicação: nada aqui é
importado por `app/`, e o `requirements.txt` da aplicação não muda.

Escopo: só o lado do sistema (relatório de lançamentos do ERP), PDF com camada
de texto (escaneado fica fora) e sem LLM.

## Instalar

```bash
pip install -r scripts/spike_pdf/requirements.txt
```

## Amostras sintéticas

```bash
python -m scripts.spike_pdf.gerar_sinteticos
```

Gera em `scripts/spike_pdf/sinteticos/` os layouts A, B e C e um PDF só com
imagem, cada um com o gabarito ao lado. Dados inventados.

## Amostras reais

Coloque cada PDF em `scripts/spike_pdf/amostras/` com o gabarito ao lado,
com o mesmo nome:

- `<amostra>.gabarito.csv`: `data;descricao;valor`, UTF-8, data AAAA-MM-DD,
  valor com ponto e negativo para saída;
- ou `<amostra>.gabarito.json` com `{"total": N}`, quando o ERP não exporta
  planilha (medição fraca: só compara quantidades).

A pasta `amostras/` e os relatórios gerados a partir dela estão no
`.gitignore`: amostra real, mesmo anonimizada, não é versionada.

Para converter a exportação do ERP (Excel ou CSV) em gabarito:

```bash
python -m scripts.spike_pdf.converter_gabarito --arquivo erp.xlsx \
    --data "Data" --descricao "Histórico" --valor "Valor" --dc "D/C" \
    --gabarito scripts/spike_pdf/amostras/erp_x.gabarito.csv
```

Valor com sinal (`--valor`), valor e coluna C/D (`--valor` e `--dc`) ou duas
colunas (`--entrada` e `--saida`). `--formato-data` (padrão `%d/%m/%Y`),
`--separador`, `--codificacao`, `--pular` e `--planilha` para ajustar por ERP.

Se um layout precisar de sinônimos de cabeçalho ou de linhas a ignorar, copie
`layouts/exemplo.toml` para `amostras/<amostra>.layout.toml`. O relatório
marca essas amostras como "configuração específica".

## Avaliar

```bash
python -m scripts.spike_pdf.avaliar --amostras scripts/spike_pdf/amostras \
    --estrategia todas --saida scripts/spike_pdf/amostras/relatorio
```

Estratégias: `tabela` (`extract_tables`), `texto` (palavras com posição) ou
`todas`. `--limiar` ajusta a similaridade mínima das descrições (padrão 0,8).
A saída tem `relatorio.md`, `relatorio.json` e, por amostra e estratégia, um
`.diferencas.csv` com as linhas que faltaram, as que sobraram e as com valor
errado ou sinal invertido. O terminal mostra só números.
