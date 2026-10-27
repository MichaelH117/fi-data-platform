"""'Results at a glance' proof tiles (reports/showcase.py): every number matches the run artifacts.

1. On a synthetic set of artifacts (tmp runs dir, dbt results, pytest record), the tiles show exactly the
   counts in those files, compact numbers are truncated (never rounded up), and nothing is invented when
   an artifact is missing.
2. On the real repo, when a site and docs/showcase_stats.json have been built: the tiles printed on
   start.html and index.html equal the JSON, and the JSON equals a fresh read of the artifacts.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from reports import showcase

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "20990101T000000Z"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def artifacts(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setenv(showcase.RUNS_ENV, str(runs))
    _write(runs / f"{RUN_ID}.json", {
        "run_id": RUN_ID, "status": "ok", "seconds": 594.67, "args": {"only": None, "start": None},
        "started_at": "2099-01-01T00:00:00+00:00", "finished_at": "2099-01-01T00:09:54+00:00",
        "steps": [{"name": n, "status": "ok"} for n in ("generate", "ingest", "dbt", "reports")]})
    _write(runs / "20990102T000000Z.json", {"run_id": "20990102T000000Z", "status": "ok", "seconds": 5.0,
                                           "args": {"only": "reports"}, "steps": []})  # partial: ignored
    res = ([{"unique_id": f"test.p.t{i}", "status": "pass"} for i in range(7)]
           + [{"unique_id": "test.p.warn_x", "status": "warn", "failures": 30},
              {"unique_id": "unit_test.p.u", "status": "pass"}, {"unique_id": "seed.p.s", "status": "success"},
              {"unique_id": "snapshot.p.sn", "status": "success"},
              {"unique_id": "model.p.m1", "status": "success"}, {"unique_id": "model.p.m2", "status": "success"}])
    _write(runs / RUN_ID / "dbt_build.json", {"metadata": {"generated_at": "2099-01-01T00:03:00Z"},
                                               "elapsed_time": 180.9, "results": res})
    _write(runs / "_pytest.json", {"passed": 758, "failed": 0, "skipped": 3, "errors": 0, "deselected": 2,
                                    "collected": 761, "selection": "-q", "seconds": 60.0,
                                    "recorded_at": "2099-01-01T01:00:00Z"})
    _write(runs / "_gates.json", {"dbt": {"status": "ok", "run_id": RUN_ID},
                                   "reconcile": {"status": "ok", "run_id": "older"}})
    monkeypatch.setattr(showcase, "_legacy", lambda: {
        "questions": 10, "legacy_wrong_or_failed": 10, "modern_errors_or_timeouts": 0, "measured_at": "x",
        "slow_query": {"id": "q07", "question": "Scoped lookup", "observed_failure": "slow", "legacy_ms": 961.99,
                       "modern_ms": 44.66, "legacy_budget": "2s"},
        "legacy_timeouts": [{"id": "q10", "question": "Match rate", "budget": "60s", "modern_ms": 32.2}],
        "source": "snapshot"})
    return tmp_path


class FakeCon:
    """Answers the three audit queries showcase._audit sends."""

    def execute(self, sql, params):
        assert params == [RUN_ID]
        rows = {"layer_row_count": [("raw", False, 2, 1_000_000), ("marts", False, 3, 13_954_790),
                                    ("staging", True, 4, 999_999), ("marts", True, 1, 1)],
                "diagnostics_result": [("pass", 16)],
                "reconciliation": [("pass", 36), ("not_migrated", 1)]}
        return type("R", (), {"fetchall": lambda self: next(v for k, v in rows.items() if k in sql)})()


def test_tiles_show_exactly_the_artifact_counts(artifacts):
    s = showcase.collect(artifacts, con=FakeCon(), packets=150)
    assert s["run_id"] == RUN_ID  # newest *full* run; the --only reports run is skipped
    v = showcase.tile_values(showcase.tiles(s))
    assert v == {
        "proof_warehouse_rows": "14.9M",  # 14,954,790 stored rows, truncated; the 1,000,000 view rows not counted
        "proof_dbt_tests": "12 of 13",  # every build node: 2 models, 1 seed, 1 snapshot, 8 data tests, 1 unit test
        "proof_pytest": "758",
        "proof_rebuild_time": "9 min 54 s",
        "proof_gates": "0",
        "proof_legacy_wrong": "10/10 → 0",
        "proof_legacy_speed": "961.9 ms → 44.6 ms",  # truncated, never rounded up; the timeout is not a speed
        "proof_dealer_packets": "150",
    }
    subs = {k.metric_id: k.sub for k in showcase.tiles(s)}
    assert "14,954,790 rows in 5 DuckDB tables and snapshots" in subs["proof_warehouse_rows"]
    assert "(13,954,790 in the marts" in subs["proof_warehouse_rows"]
    assert subs["proof_warehouse_rows"].endswith("; 5 views not counted")
    assert subs["proof_dbt_tests"].startswith("pipeline build: 2 models, 1 snapshots, 1 seeds, 8 data tests, "
                                              "1 unit tests; 0 failed; 1 known data-quality warning (warn_x, 30 rows)")
    assert "Q07 (Scoped lookup)" in subs["proof_legacy_speed"]
    assert "legacy Q10 timed out at 60 s (new: 32.2 ms)" in subs["proof_legacy_speed"]
    assert "2 slow end-to-end tests not in this record" in subs["proof_pytest"]
    assert "16 of 16 diagnostics" in subs["proof_gates"] and "1 documented waiver" in subs["proof_gates"]
    assert subs["proof_gates"].endswith("1 of 1 publish gates ok")
    assert s["gates"] == {"ok": 1, "total": 1, "names": ["dbt"], "source": "data/runs/_gates.json"}
    assert all(k.metric_id.startswith("proof_") for k in showcase.tiles(s))


def test_missing_artifacts_show_na_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.setenv(showcase.RUNS_ENV, str(tmp_path / "none"))
    monkeypatch.setattr(showcase, "_legacy", lambda: None)
    v = showcase.tile_values(showcase.tiles(showcase.collect(tmp_path, con=None, packets=0)))
    assert set(v.values()) == {"n/a"}


def test_compact_never_rounds_up():
    assert showcase.compact(24_099_999) == "24.0M"
    assert showcase.compact(13_299_999) == "13.2M"
    assert showcase.compact(999) == "999"
    assert showcase.duration(594.99) == "9 min 54 s"


def test_json_carries_the_same_tiles(artifacts):
    s = showcase.collect(artifacts, con=FakeCon(), packets=150)
    out = showcase.write_json(s, artifacts / "docs" / "showcase_stats.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert {k: t["value"] for k, t in data["tiles"].items()} == showcase.tile_values(showcase.tiles(s))
    assert data["stats"]["dbt"]["build_nodes_ok"] == 12 and data["stats"]["pytest"]["passed"] == 758


# --------------------------------------------------------------------------- the real build
STATS = ROOT / "docs" / "showcase_stats.json"
SITE = ROOT / "site"
TILE = re.compile(r'<div class="kpi[^"]*" data-metric="(proof_[a-z_]+)">\s*<p class="kpi-label">[^<]*</p>\s*'
                  r'<p class="kpi-value( kpi-empty)?">([^<]*)</p>')


def _site_tiles(page: Path) -> dict[str, str]:
    # an empty value renders as "Not enough data yet" (templates/_components.html.j2), stored as n/a
    return {m: "n/a" if empty else html.unescape(v) for m, empty, v in TILE.findall(page.read_text(encoding="utf-8"))}


@pytest.mark.warehouse
def test_built_site_tiles_match_json_and_artifacts():
    if not (STATS.exists() and (SITE / "start.html").exists()):
        pytest.skip("site or docs/showcase_stats.json not built (python -m reports)")
    data = json.loads(STATS.read_text(encoding="utf-8"))
    want = {k: t["value"] for k, t in data["tiles"].items()}
    for page in ("start.html", "index.html"):
        assert _site_tiles(SITE / page) == want, f"{page} tiles differ from {STATS.name}"
    runs = showcase.runs_dir(ROOT)
    if not runs.is_dir():
        pytest.skip("no data/runs artifacts on this machine")
    duckdb = pytest.importorskip("duckdb")
    from reports import WAREHOUSE_PATH

    if not WAREHOUSE_PATH.exists():
        pytest.skip("no warehouse")
    with duckdb.connect(str(WAREHOUSE_PATH), read_only=True) as con:
        fresh = showcase.collect(ROOT, con=con, packets=data["stats"]["dealer_packets"])
    packets = sum(1 for _ in (SITE / "dealers").glob("*/*/index.html"))
    assert data["stats"]["dealer_packets"] == packets
    stale = sorted(k for k in fresh if fresh[k] != data["stats"].get(k))
    assert not stale, f"{STATS.name} is older than the run artifacts for {stale}: rebuild with python -m reports"
