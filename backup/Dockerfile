# Imagem do serviço de backup do Postgres para o Cloudflare R2 (issue #103, ADR-018).
#
# Serviço próprio no Railway, separado da API: aponte RAILWAY_DOCKERFILE_PATH
# para backup/Dockerfile, configure o cron (0 6 * * *, UTC) e as Watch Paths
# (backup/** e scripts/backup_r2.py) no painel. O build usa a raiz do
# repositório como contexto. Testada pelo workflow .github/workflows/backup.yml.
#
# A base é a imagem oficial do Postgres 18, do Docker Hub, só pelo cliente: o
# pg_dump tem de ser da major do banco do Railway. Nenhum servidor sobe aqui, por
# isso o ENTRYPOINT da imagem é zerado. Na CI o download é feito com login
# (issue #127); no Railway, só acontece a cada deploy, não a cada execução.
FROM postgres:18

RUN apt-get update \
    && apt-get install --yes --no-install-recommends age python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY backup/requirements.txt /tmp/requirements-backup.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-backup.txt \
    && rm /tmp/requirements-backup.txt

COPY scripts/backup_r2.py /app/scripts/backup_r2.py

WORKDIR /app
USER postgres
ENTRYPOINT []
CMD ["/opt/venv/bin/python", "/app/scripts/backup_r2.py"]
