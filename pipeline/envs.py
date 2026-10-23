"""Local dev / UAT / prod environments for ``python -m pipeline.run --env``.

Each environment is a separate set of paths, so a UAT build never touches prod output:

    env   warehouse                        dbt target       sources, site, publish, runs
    prod  data/warehouse/warehouse.duckdb  dbt/target       data/source, data/landing, site, data/publish, data/runs
    uat   data/env/uat/warehouse.duckdb    dbt/target_uat   data/env/uat/{source,landing,site,publish,runs}
    dev   data/env/dev/warehouse.duckdb    dbt/target_dev   data/env/dev/{source,landing,site,publish,runs}

UAT and dev start from a copy of the prod sources (the local stand-in for a Snowflake zero-copy clone,
``warehouse/snowflake/07_environments_clone.sql``), so the feed and ``--append-day`` steps write only
their own copy. ``tools/env_diff.py`` compares the UAT and prod warehouses before a promotion
(docs/operating-model/RELEASE.md).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENVS: tuple[str, ...] = ("dev", "uat", "prod")
log = logging.getLogger("pipeline")


@dataclass(frozen=True)
class EnvPaths:
    name: str
    admin_core: Path
    legacy: Path
    landing: Path
    warehouse: Path
    dbt_target: str  # relative to the dbt project dir (docs/V3_DESIGN.md 4.2)
    dbt_logs: Path
    publish: Path
    site: Path
    runs: Path

    def variables(self) -> dict[str, str]:
        return {
            "ADMIN_CORE_PATH": str(self.admin_core), "LEGACY_DB_PATH": str(self.legacy),
            "LANDING_DIR": str(self.landing), "WAREHOUSE_PATH": str(self.warehouse),
            "DBT_TARGET_PATH": self.dbt_target, "DBT_LOG_PATH": str(self.dbt_logs),
            "PUBLISH_DIR": str(self.publish), "PACKETS_DIR": str(self.publish / "dealers"),
            "SITE_DIR": str(self.site), "PIPELINE_RUNS_DIR": str(self.runs), "PIPELINE_ENV": self.name,
        }


def env_paths(name: str, root: Path = ROOT) -> EnvPaths:
    if name not in ENVS:
        raise ValueError(f"unknown environment {name!r}; pick one of {', '.join(ENVS)}")
    data = root / "data"
    if name == "prod":
        return EnvPaths("prod", data / "source" / "admin_core.db", data / "source" / "legacy_admin.db",
                        data / "landing", data / "warehouse" / "warehouse.duckdb", "target", root / "dbt" / "logs",
                        data / "publish", root / "site", data / "runs")
    base = data / "env" / name
    return EnvPaths(name, base / "source" / "admin_core.db", base / "source" / "legacy_admin.db", base / "landing",
                    base / "warehouse.duckdb", f"target_{name}", root / "dbt" / f"logs_{name}",
                    base / "publish", base / "site", base / "runs")


def seed_sources(env: EnvPaths, prod: EnvPaths) -> list[Path]:
    """Copy the prod sources into a non-prod environment once (never overwrites). Returns what was copied."""
    copied: list[Path] = []
    for src, dst in ((prod.admin_core, env.admin_core), (prod.legacy, env.legacy)):
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst)
    if prod.landing.is_dir() and not env.landing.exists():
        shutil.copytree(prod.landing, env.landing)
        copied.append(env.landing)
    return copied


def activate(name: str, environ: MutableMapping[str, str] = os.environ, root: Path = ROOT) -> EnvPaths:
    """Point every path variable at the environment ``name`` (the selection wins over stray values)."""
    env = env_paths(name, root)
    if name != "prod":
        for p in seed_sources(env, env_paths("prod", root)):
            log.info("env %s: seeded %s from prod", name, p)
        env.runs.mkdir(parents=True, exist_ok=True)
    for key, value in env.variables().items():
        if environ.get(key) not in (None, value):
            log.info("env %s overrides %s=%s", name, key, environ[key])
        environ[key] = value
    return env
