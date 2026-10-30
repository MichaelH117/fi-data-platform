# Presentation Layer Research: Daily Stakeholder Reports Without Power BI

*Research spike, 2026-10-22. Goal: free replacement for Power BI.*

## Power BI concepts and their open-source equivalents

| Power BI concept | What it does | Open-source equivalent |
|---|---|---|
| Semantic model + DAX measures | One governed definition of "loss ratio" | **dbt models + metrics YAML** (Lightdash reads them directly), or Superset dataset metrics |
| Scheduled refresh | Daily data reload | **Orchestrator (cron / GitHub Actions / Airflow / Dagster) runs ELT, then dbt**. Dashboards query live or rebuild |
| Apps with audiences | Different report sets for different groups | Superset/Metabase **roles + collections**; Lightdash **spaces + groups** |
| Row-level security (RLS) | Dealer A sees only Dealer A's rows | Superset RLS filters (free); Lightdash user attributes; **Postgres RLS** (works under any tool) |
| Subscriptions | Emailed PDF or image snapshots | Superset Alerts & Reports; Metabase subscriptions; Lightdash scheduled deliveries |

## Tool comparison

| Tool | Semantic layer | Refresh | Dealer vs exec audiences | Embedding | Email/PDF | Hosting cost |
|---|---|---|---|---|---|---|
| **Apache Superset** | Datasets with saved metrics (medium) | Live SQL + cache | **RLS is built in and free**, plus roles | iframe/SDK, guest tokens | Alerts & Reports (needs Celery + headless browser) | Free to self-host. Docker needs about 4 GB RAM |
| **Metabase OSS** | Models/metrics (light) | Live + caching | Collections and groups. **Sandboxing/RLS is Pro only (~$500+/mo)** | Static embed (has a badge on OSS) | Dashboard subscriptions | Free, one container |
| **Lightdash** | **dbt-native: metrics live in dbt YAML** (best match for Power BI measures) | Live | Spaces, groups, user-attribute RLS | Some features are commercial tier | Scheduled deliveries (some are commercial tier) | Free to self-host, or cloud trial |
| **Evidence.dev** | SQL + Markdown in git | **Static build. Schedule rebuilds with GitHub Actions cron** | None natively (would need separate builds per audience) | Static HTML anywhere | No | **$0 on GitHub Pages / Netlify** |
| **Streamlit** | None (you write it in Python) | Your code / cache TTL | Hand-rolled (`st.login` OIDC) | iframe | No | Free Community Cloud |
| **Grafana** | None (SQL panels) | Live, auto-refresh | Orgs/teams | Public dashboards | **PDF reporting is Enterprise/Cloud only** | Free |

## RECOMMENDATION

**Primary: Apache Superset (Docker Compose) on top of Postgres marts plus a dbt semantic layer.**
- It maps one-to-one onto the Power BI pieces: governed datasets and metrics, dashboards grouped by audience, **free row-level security** (`dealer_id = {{ current_username }}`-style filters for dealer logins vs unrestricted exec roles), and scheduled email/PDF reports.
- Define loss ratio, claim frequency and severity **once in dbt** (`fct_claims`, `fct_contracts`, `mart_dealer_loss_ratio`). Superset only consumes the marts, so the semantic layer stays tool-agnostic. That is the "data broker" product story.
- Daily refresh: orchestrator runs ingest, then `dbt build` (with tests), then a Superset cache warm-up or report trigger. Document the full DAG.
- Back it with **Postgres RLS policies** as defense in depth. Superset RLS has had reported bypass issues ([OX Security](https://www.ox.security/blog/https-www-ox-security-critical-access-control-flaw-in-apache-superset-exposes-sensitive-data-to-unauthorized-users/)), and a database-level policy adds a second layer.

**Secondary: an Evidence.dev public demo site.** It rebuilds daily through GitHub Actions cron and deploys free to GitHub Pages. Superset needs a running server, so it is hard to keep a free live demo up. Evidence gives a **clickable public link**: an executive "Loss Ratio Daily" report generated from synthetic data. Use `build:strict` so a broken query never publishes ([Evidence deployment](https://docs.evidence.dev/deployment/overview), [GitHub Pages guide](https://docs.evidence.dev/deployment/self-host/github-pages)).

**Alternative to the primary:** if the team adopts dbt heavily, swap Superset for **Lightdash**, since metrics in dbt YAML are the closest analog to DAX measures. Before committing, check which features (scheduled deliveries, RLS) are open-source rather than commercial tier.

**Skip:** Grafana (wrong genre, PDF is paid), Metabase OSS (RLS is paywalled, which is the core dealer requirement), and Streamlit as the main BI layer. Streamlit could still serve as a small "data request / broker storefront" add-on.

## Tradeoffs
- Superset: heavier setup (Redis, Celery workers, headless Chrome for PDFs) and no free hosting. Mitigate with a one-command `docker compose up`, screenshots or GIFs in the README, and the Evidence site as the live demo.
- Evidence: no per-user security, so the public site must use **synthetic, aggregated data only**.
- Two tools means two places to define charts. Keeping the metrics in dbt avoids metric drift between them.

## Sources
- [Metabase pricing](https://metabase.com/pricing), [Metabase upgrade (sandboxing = Pro)](https://metabase.com/upgrade), [Coefficient: Metabase pricing 2026](https://coefficient.io/metabase-pricing)
- [Railway: Metabase RLS notes](https://railway.com/deploy/metabase-rls-notes)
- [Preset (managed Superset)](https://preset.io/product), [Superset semantic layer overview](https://geosurge.ai/corpus/gen-3168/apache-superset/apache-superset-semantic-layer-and-dataset-modeling.html), [Metabase vs Superset](https://www.modern-datatools.com/compare/metabase-vs-superset)
- [Lightdash user attributes](https://docs.lightdash.com/references/user-attributes), [Lightdash scheduled deliveries](https://www.lightdash.com/blogpost/demo-week-feature-day-2023-04-14), [Lightdash on AWS Marketplace (commercial tiers)](https://aws.amazon.com/marketplace/pp/prodview-tyddgzq6faftq)
- [Evidence deployment overview](https://docs.evidence.dev/deployment/overview), [Evidence on GitHub Pages](https://docs.evidence.dev/deployment/self-host/github-pages)
- Grafana PDF reporting (Enterprise): https://grafana.com/docs/grafana/latest/dashboards/create-reports/
