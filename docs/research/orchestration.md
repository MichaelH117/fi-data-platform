# Orchestration research spike: Airflow vs Dagster vs Prefect vs GitHub Actions cron

*Date: 2026-10-22. Scope: a daily batch pipeline (ingest -> transform -> test -> publish reports) for a solo or small-team project. It runs locally in Docker, with CI as an option.*

## RECOMMENDATION

**Use Apache Airflow 3.x (LocalExecutor, Docker Compose) as the orchestrator. Use GitHub Actions only for CI** (lint, unit tests, `dbt build` against a throwaway Postgres on each PR). Do not use Actions as the production scheduler.

Reasons:
1. **Market adoption.** Airflow is still the orchestrator that job postings name most often. It has the largest ecosystem, about 30M monthly downloads and roughly 80k organizations according to Astronomer. Estimates of its share of postings vary widely (15.8% to 48%, depending on the dataset), and UK posting counts fell year over year.
2. **The asset gap has narrowed.** Airflow 3.0 (April 2025) made **assets** first-class, so you can still show asset-aware, data-driven scheduling. Dagster's lineage is still richer.
3. **dbt lineage.** Running dbt through `astronomer-cosmos` turns each dbt model and test into its own Airflow task, so the graph shows lineage at the model level.
4. **Ecosystem stability.** Prefect agreed to acquire Dagster Labs (announced July 2026). Dagster OSS is promised to continue, but its roadmap is uncertain for now. Airflow is an Apache project with no single vendor behind it.

**Runner-up: Dagster.** It is technically the best fit for "warehouse + dbt + lineage + data quality" (asset graph, asset checks, `dagster-dbt`), and its local developer experience is the best of the four. Choose it if the priority is a modern asset-centric data platform rather than the most widely adopted tool. The DAG code stays small either way, so switching later is cheap.

## Comparison

| | Airflow 3 | Dagster | Prefect 3 | GitHub Actions cron |
|---|---|---|---|---|
| Mental model | DAGs of tasks (+ assets in 3.x) | Software-defined assets | Plain Python `@flow`/`@task` | YAML jobs on a cron |
| Market adoption | **Highest** | Growing, niche | Moderate | Low (CI tool, not orchestration) |
| Local Docker setup | Heaviest: api-server, scheduler, dag-processor, Postgres (`airflow standalone` for dev) | Moderate: webserver, daemon, code location, Postgres (official compose example) | Lightest: `prefect server start` + worker | None, but not local |
| Observability | Good UI, task logs, retries, SLAs/callbacks | Best: asset catalog, materialization history, checks | Good UI, logs, retries | Basic run logs only |
| Asset lineage | Assets + Cosmos dbt graph; OpenLineage provider | **Native, first-class** | Limited (artifacts/events) | None |
| Daily scheduling reliability | Reliable | Reliable | Reliable | Can be delayed at peak times; **auto-disabled after 60 days without activity** on public repos |
| Risk | Operational weight | Vendor change (Prefect acquisition) | Smaller adoption than Airflow | Not a real orchestrator |

## Tradeoffs to accept with Airflow
- More containers and more memory. Mitigate with LocalExecutor (no Celery or Redis) and one Postgres container shared with the warehouse in dev.
- The DAG and task model is more verbose than Dagster assets. Mitigate with the TaskFlow API and Cosmos for dbt.
- Lineage is less visual than Dagster's. Mitigate by emitting Airflow assets per mart table, and optionally using the OpenLineage provider with Marquez as a stretch goal.

## Suggested shape
`ingest_raw` (Python, load contracts to raw schema) -> `dbt_build` via Cosmos (staging -> marts, with tests as tasks) -> `publish_reports` (refresh report extracts/dashboards), scheduled `@daily`, with assets on the mart tables. GitHub Actions runs `ruff`, `pytest`, `dbt build` and a DAG-import test on every PR.

## Sources
- [Astronomer: Airflow vs Dagster vs Prefect](https://llms.astronomer.io/airflow-vs-dagster-vs-prefect) (vendor, pro-Airflow)
- [ZenML: Orchestration showdown](https://www.zenml.io/blog/orchestration-showdown-dagster-vs-prefect-vs-airflow)
- [Tech Insider: Airflow vs Dagster vs Prefect 2026](https://tech-insider.org/airflow-vs-dagster-vs-prefect-2026/)
- [Automation Atlas: Prefect vs Airflow 2026](https://automationatlas.io/answers/prefect-vs-apache-airflow-2026/)
- [Dagster blog: Prefect is acquiring Dagster](https://dagster.io/blog/prefect-is-acquiring-dagster), [Dagster customer FAQ](https://dagster.io/dagster-customer-faq)
- [Dagster docs: Deploy with Docker](https://docs.dagster.io/examples/deploy_docker), [dagster-dbt API](https://docs.dagster.io/api/libraries/dagster-dbt)
- [ITJobsWatch: Apache Airflow UK job trends](https://www.itjobswatch.co.uk/jobs/uk/apache%20airflow.do)
- [GitHub Docs: Disabling and enabling a workflow (60-day rule)](https://docs.github.com/en/enterprise-server@3.5/actions/using-workflows/disabling-and-enabling-a-workflow)
- [Crontap: GitHub Actions cron drift](https://crontap.com/blog/github-actions-cron-drift-problem)

*Caveats: version numbers differ between secondary sources (Airflow 3.3.x, Dagster 1.13.x and Prefect 3.8.x were reported in September 2026). Job-share statistics conflict. I did not confirm whether the Prefect–Dagster deal has closed.*
