# Local PostgreSQL target (opt-in)

A real SQL server for Power BI (or any SQL client) to read. The default pipeline does **not** need it:
CI and `python -m pipeline.run` stay on DuckDB only, and the Postgres tests skip when no server answers.

## One-time setup (no admin rights, no Docker)

1. Download the official EDB **PostgreSQL 16 Windows x64 binaries zip** (enterprisedb.com,
   "PostgreSQL binaries") and unzip it, for example to `C:\Users\<you>\tools\pgsql`.
2. Create a cluster (UTF8, scram-sha-256):
   `pgsql\bin\initdb -D <data dir> -U postgres -E UTF8 --locale=C --auth=scram-sha-256 --pwfile=<file with a random password>`
   then add `listen_addresses = '127.0.0.1'` and `port = 5432` to `<data dir>\postgresql.conf`.
3. Copy `.env.example` to `.env` (git-ignored) and fill in the paths and two random passwords
   (superuser, and the read-only `bi_reader` login).
4. `pip install -r requirements-postgres.txt` (psycopg).

## Daily use

```powershell
powershell -ExecutionPolicy Bypass -File tools\pg.ps1 start        # stop | restart | status | psql [-Superuser]
.venv\Scripts\python -m pipeline.run --postgres                     # full run, then sync + reconcile
.venv\Scripts\python -m warehouse.postgres sync                     # or just publish the current warehouse
.venv\Scripts\python -m warehouse.postgres reconcile                # re-check counts and totals
.venv\Scripts\python -m warehouse.postgres status
```

## What `sync` does

- Copies every table and view of `core, finance, dealer, claims, regulatory, actuarial, gold,
  gold_actuarial, seeds` from DuckDB into database `vsc_analytics`, same schema and table names
  (views land as tables).
- Primary keys from the dbt `unique` / `unique_combination` tests (checked unique in DuckDB first), and a
  b-tree index on every other `*_key` column.
- Applies [rls.sql](rls.sql), loads `security.user_dealer_access` from `core.dim_user_dealer_access`, and
  attaches the tenant/dealer row policy to every table with a `tenant_key`.
- `bi_reader` is a read-only LOGIN with SELECT on the published schemas and `BYPASSRLS`: it is the Power BI
  import login and must read every row (the semantic model's own RLS roles scope report viewers). Analysts and
  app logins get role `reporter` and stay under the row policies. bi_reader cannot write or read the access map.
- Runs in **one transaction**: row counts (exact), integer sums (exact) and float/dollar sums (to the
  cent) are reconciled against DuckDB before COMMIT. Any mismatch rolls back and exits 1, so readers keep
  the last good copy. Re-running is idempotent; tables removed from DuckDB are dropped.

## Power BI

`python -m semantic --target postgres` writes `semantic/model_postgres/` (same measures, calc group and
RLS as `semantic/model/`, M sources `PostgreSQL.Database(PostgresServer, PostgresDatabase)`, Import
mode). Sign in with Database authentication as `bi_reader`; the password is in `.env`.
