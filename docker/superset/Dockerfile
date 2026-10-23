# Superset with Postgres + DuckDB drivers. UNTESTED (no Docker on the dev box).
FROM apache/superset:4.1.2

USER root
RUN pip install --no-cache-dir psycopg2-binary duckdb-engine==0.17.0 duckdb==1.4.1
USER superset

# First boot: create admin, migrate metadata db, then serve.
CMD ["/bin/sh", "-c", "superset db upgrade && superset fab create-admin --username admin --firstname a --lastname a --email admin@example.com --password \"$SUPERSET_ADMIN_PASSWORD\" || true; superset init && /usr/bin/run-server.sh"]
