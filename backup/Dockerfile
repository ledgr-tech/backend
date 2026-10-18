# Imagem do serviço de backup do Postgres para o Cloudflare R2 (issue #103, ADR-018).
#
# Serviço próprio no Railway, separado da API: aponte RAILWAY_DOCKERFILE_PATH
# para backup/Dockerfile e configure o cron (0 6 * * *, UTC) no painel. O build
# usa a raiz do repositório como contexto.
#
# A base é a imagem oficial do Postgres 18 (espelho público da AWS, o mesmo da
# CI desde o PR #109) só pelo cliente: o pg_dump tem de ser da major do banco
# do Railway. Nenhum servidor sobe aqui, por isso o ENTRYPOINT da imagem é zerado.
FROM public.ecr.aws/docker/library/postgres:18

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
