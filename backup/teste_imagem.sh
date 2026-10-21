#!/bin/sh
# Teste da imagem do backup (issue #103): roda dentro do container, com o
# pg_dump e o age de verdade, contra um Postgres de TESTE (o da CI, ou o local
# da suíte). Nunca contra produção.
#
#   docker run --rm --network host --user "$(id -u)" -v "$PWD/saida":/out \
#     -v "$PWD/backup/teste_imagem.sh":/teste.sh:ro \
#     -e BANCO_TESTE=postgresql://ci:ci@localhost:5432/ci ledgr-backup sh /teste.sh
#
# Gera uma chave descartável, faz o dump criptografado pelo mesmo
# gerar_dump_cifrado do serviço, decifra e confere que o pg_restore lê o
# arquivo e que a tabela alembic_version está nele.
set -eu

: "${BANCO_TESTE:?defina BANCO_TESTE com a URL de um Postgres de teste}"
OUT=/out

age-keygen -o "$OUT/chave.txt" 2>/dev/null
RECIPIENT=$(age-keygen -y "$OUT/chave.txt")

cd /app
RECIPIENT="$RECIPIENT" /opt/venv/bin/python - <<'EOF'
import os
from pathlib import Path

from scripts.backup_r2 import ler_config, gerar_dump_cifrado

cfg = ler_config({
    "DATABASE_URL": os.environ["BANCO_TESTE"],
    "R2_ENDPOINT": "https://teste.invalid",
    "R2_BUCKET": "teste",
    "R2_ACCESS_KEY_ID": "teste",
    "R2_SECRET_ACCESS_KEY": "teste",
    "BACKUP_AGE_RECIPIENT": os.environ["RECIPIENT"],
    "HEALTHCHECK_URL": "https://teste.invalid",
})
tamanho = gerar_dump_cifrado(cfg, Path("/out/x.dump.age"))
print(f"dump criptografado: {tamanho} bytes")
EOF

# O arquivo enviado não pode ser um dump legível.
if head -c 5 "$OUT/x.dump.age" | grep -q PGDMP; then
  echo "FALHA: o arquivo não está criptografado" >&2
  exit 1
fi

age --decrypt --identity "$OUT/chave.txt" --output "$OUT/x.dump" "$OUT/x.dump.age"
pg_restore --list "$OUT/x.dump" > "$OUT/lista.txt"
grep -q "TABLE public alembic_version" "$OUT/lista.txt"
echo "OK: dump criptografado, decifrado e lido pelo pg_restore"
