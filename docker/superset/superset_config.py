"""Superset config for the server-target stack. UNTESTED (no Docker on the dev box).

Dashboards sit on the marts (core, finance, claims, dealer, regulatory). Dealer users get
row-level security from the warehouse itself (warehouse/postgres/rls.sql): connect Superset as an
``rls_app`` member and set ``app.user_name`` per user (for example in a DB_CONNECTION_MUTATOR), so the
same access map filters every tool. Or use Superset's own RLS filters on dealer_key.
Exec users see the full book.
"""

import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"  # Superset's own metadata
FEATURE_FLAGS = {"DASHBOARD_RBAC": True, "ROW_LEVEL_SECURITY": True}
# Warehouse connections (add in the UI): postgresql://vsc:...@warehouse:5432/vsc
# or duckdb:////app/warehouse/warehouse.duckdb?access_mode=read_only
