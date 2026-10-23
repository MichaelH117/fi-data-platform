# Airflow 3 image with dbt + Cosmos and the repo's Python deps. UNTESTED (no Docker on the dev box).
FROM apache/airflow:3.1.0-python3.12

COPY requirements.txt requirements-airflow.txt /tmp/
# Airflow pins its own tree: install against the official constraints file.
RUN pip install --no-cache-dir -r /tmp/requirements-airflow.txt \
      --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.1.0/constraints-3.12.txt \
 && pip install --no-cache-dir dbt-duckdb==1.10.0 faker==37.11.0 jinja2 plotly==6.3.1

WORKDIR /opt/airflow/repo
