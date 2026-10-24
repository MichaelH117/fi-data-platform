"""The analyst SQL playbook (sql/playbook): offline style checks always; a live run when Postgres is up.

The live test runs the manifest against the already-built legacy databases (``python -m legacy build``);
it skips when the server is down or the legacy database does not exist (CI has neither).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from legacy import PLAYBOOK_DIR, SEMANTIC_DIR
from legacy import playbook as pb

MANIFEST = PLAYBOOK_DIR / "manifest.yml"
SPEC = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["queries"]
FILES = sorted(PLAYBOOK_DIR.glob("*.sql"))
# the brief's coverage list: one file each (prefix -> pattern)
COVERAGE = {"01": "catalog", "02": "freshness", "03": "dim match", "04": "key-bridge", "05": "claim-key",
            "06": "fan-out", "07": "funded-reserve", "08": "claim-grain", "09": "cancel export",
            "10": "coverage-cost lineage", "11": "duplicate-identity", "12": "EAV", "13": "producer vs provider",
            "14": "loss ratio"}


def _code(sql: str) -> str:
    """The SQL without comments and string / quoted-identifier literals."""
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:[^']|'')*'|\"[^\"]*\"", "", sql)


def test_manifest_lists_every_file_once():
    listed = [q["file"] for q in SPEC]
    assert len(listed) == len(set(listed))
    assert sorted(listed) == [f.name for f in FILES]
    for q in SPEC:
        assert q.get("database", "legacy") in {"legacy", "legacy_admin", "modern"}
        assert pb.seconds(q.get("timeout", pb.DEFAULT_TIMEOUT)) <= 300
        assert (".modern." in q["file"]) == (q.get("database") == "modern") == bool(q.get("optional"))


def test_brief_coverage_one_file_per_pattern():
    for prefix, word in COVERAGE.items():
        legacy_files = [f for f in FILES if f.name.startswith(prefix + "_") and ".modern." not in f.name]
        assert len(legacy_files) == 1, prefix
        assert word.lower() in pb.header(legacy_files[0].read_text(encoding="utf-8"))["pattern"].lower(), prefix
    dax = [f for f in FILES if "_dax_" in f.name and ".modern." not in f.name]
    assert 3 <= len(dax) <= 5


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_file_style(path: Path):
    sql = path.read_text(encoding="utf-8")
    h = pb.header(sql)
    assert {"pattern", "question", "trap"} <= set(h), "header must state pattern, question and trap"
    body = [ln for ln in sql.splitlines() if ln.strip() and not ln.startswith("--")]
    assert re.fullmatch(r"set statement_timeout = '\d+(s|min)';", body[0]), "set statement_timeout first"
    code = _code(sql)
    assert not re.search(r"\b(SELECT|FROM|WHERE|JOIN|GROUP BY|ORDER BY|WITH|AS|AND|OR)\b", code), "lowercase keywords"
    assert "information_schema." not in code, "discover through pg_catalog"
    for clause in re.findall(r"group by ([^;)]*?)(?: order by| having|\)|;|$)", code, re.M):
        assert re.fullmatch(r"\d+(, \d+)*", clause.strip()), f"positional group by: {clause!r}"
    if ".modern." not in path.name and path.name[:2] not in {"12"}:
        assert '"' in sql, "legacy columns are quoted CamelCase"


def test_discovery_and_probe_techniques_present():
    text = {f.name[:2]: f.read_text(encoding="utf-8") for f in FILES if ".modern." not in f.name}
    assert "pg_catalog.pg_attribute" in text["01"] and "has_table_privilege" in text["01"]
    assert "tablesample system" in text["02"] and "repeatable" in text["02"]
    heavy = ("04", "06", "07", "10", "14", "17")
    assert all(re.search(r"^explain ", text[k], re.M) for k in heavy)


def test_dax_translations_quote_real_generated_measures():
    tmdl = "\n".join(p.read_text(encoding="utf-8") for p in SEMANTIC_DIR.rglob("*.tmdl"))
    measures = set(re.findall(r"measure (.+? = .+)", tmdl))
    for f in FILES:
        if "_dax_" not in f.name or ".modern." in f.name:
            continue
        quoted = re.findall(r"^--\s{5,}(\S.*? = (?:SUM|COUNTROWS|CALCULATE|DISTINCTCOUNT)\(.*\))\s*$",
                            f.read_text(encoding="utf-8"), re.M)
        assert quoted, f.name
        for q in quoted:
            assert re.sub(r"\s+=\s+", " = ", q) in measures, f"{f.name}: {q} is not a generated measure"


def test_runner_helpers(tmp_path):
    assert pb.seconds("30s") == 30 and pb.seconds("5min") == 300 and pb.seconds("1500ms") == 1.5
    assert pb.seconds(90) == 90
    with pytest.raises(ValueError):
        pb.seconds("soon")
    h = pb.header("-- pattern:  a\n-- question: b\n--           c\n-- trap:     d\nselect 1;\n")
    assert h == {"pattern": "a", "question": "b c", "trap": "d"}
    rows = [{"status": "PASS", "file": "a.sql", "database": "x", "rows": 3, "expected_rows": [1, 5], "ms": 1.0,
             "ok": True, "error": None, "timed_out": False, "timeout": "1s"},
            {"status": "FAIL", "file": "b.sql", "database": "x", "rows": None, "expected_rows": 1, "ms": 1000.0,
             "ok": False, "error": None, "timed_out": True, "timeout": "1s"}]
    pb.write(rows, tmp_path / "r.json", tmp_path / "r.md")
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "1 passed, 0 skipped, 1 failed" in md and "timeout after 1s" in md


def test_missing_manifest_exits_2(tmp_path):
    assert pb.main(tmp_path / "manifest.yml") == 2


# ---------------------------------------------------------------------- live run (Postgres)
def _live_reason() -> str | None:
    from legacy import build
    from warehouse.postgres.sync import available

    if (why := available()) is not None:
        return why
    try:
        return None if build.exists() else "legacy database not built (python -m legacy build)"
    except Exception as exc:  # noqa: BLE001 - any connection problem means skip
        return str(exc)


@pytest.mark.warehouse
def test_playbook_runs_clean_against_the_live_legacy_db(tmp_path):
    if (why := _live_reason()) is not None:
        pytest.skip(f"PostgreSQL legacy database unavailable: {why}")
    results = pb.run(MANIFEST)
    failed = [(r["file"], r.get("error") or r.get("rows")) for r in results if r["status"] == "FAIL"]
    assert not failed, failed
    by = {r["file"]: r for r in results}
    assert by["13_producer_vs_provider_trap.sql"]["first_row"][0] == "0"  # the trap really returns 0
    pb.write(results, tmp_path / "r.json", tmp_path / "r.md")
