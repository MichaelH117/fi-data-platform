"""Docs area: README facts match the build, the design contract's dbt path rule, one money vocabulary.

1. README counts are floors of the dbt manifest (skipped without ``dbt/target/manifest.json``).
2. README and LEGACY_VS_BETTER tell the history, partner-program scoping and environment story.
3. V3_DESIGN 4.2 gives the DBT_TARGET_PATH form that dbt and ``pipeline.diagnostics.target_dir()`` agree on.
4. ``reports.build.money`` puts the sign before the dollar sign.
5. The executive overview's premium and ratio tiles come from the P&L mart and equal the Finance
   pages' T12 figures to the cent (unit test on a tiny in-memory mart, plus the real warehouse).
6. The README names every diagnostics gate and intake source.
7. README, LEGACY_VS_BETTER and RELEASE document ``--env`` and ``tools/env_diff.py`` as the code builds them.
8. ARCHITECTURE and DATA_MODEL describe V3 (no retired V1 names; every model they name exists).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from reports.build import money, overview_money
from reports.data import load_pnl_monthly

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


# --------------------------------------------------------------------------- 1. README counts
def test_readme_dbt_counts_match_manifest():
    manifest = ROOT / "dbt" / "target" / "manifest.json"
    if not manifest.exists():
        pytest.skip("no dbt manifest; run dbt parse first")
    nodes = json.loads(manifest.read_text(encoding="utf-8"))["nodes"].values()
    counts = {k: sum(n["resource_type"] == k for n in nodes) for k in ("model", "snapshot", "seed", "test")}
    m = re.search(r"dbt Core \((\d+)\+ models, (\d+) snapshots, (\d+) seeds, (\d+)\+ data tests", README)
    assert m, "README must state dbt counts as '<n>+ models, <n> snapshots, <n> seeds, <n>+ data tests'"
    models, snapshots, seeds, tests = map(int, m.groups())
    assert snapshots == counts["snapshot"] and seeds == counts["seed"]
    # "<n>+" is a floor: true today, and not more than ~15% stale.
    for floor, actual in ((models, counts["model"]), (tests, counts["test"])):
        assert floor <= actual <= floor * 1.15, (floor, actual)


# --------------------------------------------------------------------------- 2. story
def test_readme_tells_history_scoping_and_environments():
    assert "## How we got here" in README
    for stage in ("1. App-database reporting", "2. BI over the operational database",
                  "3. Partner feeds and white-label programs", "4. Warehouse migration"):
        assert stage in README
    assert "## Scoping and environments" in README
    for term in ("LKS", "NGT", "PVA", "site/dealers/<tenant>/<dealer>/", "dbt/profiles.yml",
                 "warehouse/snowflake/07_environments_clone.sql", "ANALYTICS_UAT"):
        assert term in README, term
    for path in ("dbt/profiles.yml", "warehouse/snowflake/07_environments_clone.sql", "docs/ACCESS_MATRIX.md"):
        assert (ROOT / path).exists(), path
    legacy = (ROOT / "docs" / "LEGACY_VS_BETTER.md").read_text(encoding="utf-8")
    assert "**tenant** is a white-label partner program" in legacy
    assert "site/dealers/<tenant>/<dealer>/" in legacy


# --------------------------------------------------------------------------- 3. design contract
def test_design_dbt_target_path_resolves_where_diagnostics_looks(monkeypatch):
    design = (ROOT / "docs" / "V3_DESIGN.md").read_text(encoding="utf-8")
    assert "DBT_TARGET_PATH=dbt/target_" not in design
    assert "`DBT_TARGET_PATH=target_<role>`" in design
    from pipeline import diagnostics

    monkeypatch.setenv("DBT_TARGET_PATH", "target_docs")
    assert diagnostics.target_dir() == ROOT / "dbt" / "target_docs"


# --------------------------------------------------------------------------- 4. money format
@pytest.mark.parametrize(("value", "text"), [
    (-5689, "-$5,689"), (-12_000, "-$12K"), (-1_234_567, "-$1.2M"), (5689, "$5,689"), (12_000, "$12K"),
    (1_234_567, "$1.2M"), (0, "$0"), (-0.4, "$0"), (None, "n/a"), (float("nan"), "n/a"),
])
def test_money_sign_before_dollar(value, text):
    assert money(value) == text


# --------------------------------------------------------------------------- 5. one vocabulary
def _mini_pnl() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("create schema finance")
    con.execute("create table finance.mart_cession_experience (posting_period_key int, is_closed boolean)")
    periods = [y * 100 + m for y in (2025, 2026) for m in range(1, 13)][:21]  # 202501 .. 202609
    con.executemany("insert into finance.mart_cession_experience values (?, ?)",
                    [(k, k <= 202608) for k in periods])
    con.execute("""create table finance.mart_pnl_by_product (posting_period_key int, product_code varchar,
        written_coverage_cost double, earned_premium double, earned_claims_reserve double, incurred_claims double,
        earned_acquisition_cost double, earned_admin_fee double)""")
    rows = [(k, p, 100.0, 80.0, 60.0, 30.0 + (k % 100), 10.0, 5.0) for k in periods for p in ("A", "B")]
    con.executemany("insert into finance.mart_pnl_by_product values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def test_overview_tiles_equal_pnl_t12_sums():
    con = _mini_pnl()
    f = load_pnl_monthly(con)
    assert f["month"].iloc[-1] == pd.Timestamp("2026-08-01")  # stops at the closed period, like Finance
    kpis, sections = overview_money(SimpleNamespace(pnl_monthly=f, portfolio=None))
    by = {k.label: k for k in kpis}
    t12 = con.execute("""select sum(written_coverage_cost), sum(earned_premium), sum(earned_claims_reserve),
        sum(incurred_claims), sum(incurred_claims + earned_acquisition_cost + earned_admin_fee)
        from finance.mart_pnl_by_product where posting_period_key between 202509 and 202608""").fetchone()
    assert by["Written coverage cost, T12"].value == money(t12[0])
    assert by["Earned premium, T12"].value == money(t12[1])
    assert by["Earned claims reserve, T12"].value == money(t12[2])
    assert by["Loss ratio, T12"].value == f"{t12[3] / t12[2]:.1%}"
    assert by["Combined ratio, T12"].value == f"{t12[4] / t12[1]:.1%}"
    assert [s.title for s in sections][1] == "Trailing-12 loss ratio and combined ratio"
    # One label, one meaning: never an unqualified "Written premium" tile beside the P&L figures.
    assert not any(k.label.startswith("Written premium") for k in kpis)


def test_overview_falls_back_to_v1_figures_with_honest_labels():
    months = pd.date_range("2025-01-01", periods=13, freq="MS")
    portfolio = pd.DataFrame({"month": months, "t12_earned_premium": 100.0, "t12_loss_ratio": 0.5})
    kpis, sections = overview_money(SimpleNamespace(pnl_monthly=None, portfolio=portfolio))
    assert [k.label for k in kpis] == ["Earned claims reserve, T12", "Loss ratio, T12"] and sections == []


def test_real_warehouse_overview_equals_finance_to_the_cent():
    if not WAREHOUSE.exists():
        pytest.skip("no built warehouse")
    import reports.finance as fin

    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        try:
            f = load_pnl_monthly(con)
            # The Finance pages' own window: twelve periods ending at the latest closed period.
            closed = con.execute("select max(posting_period_key) from finance.mart_cession_experience "
                                 "where is_closed").fetchone()[0]
            t12 = con.execute("""select sum(written_coverage_cost), sum(earned_premium), sum(earned_claims_reserve),
                sum(incurred_claims) from finance.mart_pnl_by_product where posting_period_key between ? and ?""",
                              [fin.shift_period(closed, -11), closed]).fetchone()
        except duckdb.Error as exc:
            pytest.skip(f"finance marts not built: {exc}")
    now = f.iloc[-1]
    got = (now.t12_written_coverage_cost, now.t12_earned_premium, now.t12_earned_claims_reserve,
           now.t12_incurred_claims)
    assert [round(x, 2) for x in got] == [round(x, 2) for x in t12]


# --------------------------------------------------------------------------- 6. README stays current
def test_readme_names_every_diagnostics_gate_and_intake_source():
    from pipeline import diagnostics
    src = Path(diagnostics.__file__).read_text(encoding="utf-8")
    gates = sorted(set(re.findall(r'"(diag_[a-z_]+)": \(', src)))
    assert len(gates) == 16, gates
    pitch = README.split("## Legacy vs better")[0]
    assert "Sixteen\n  diagnostics gates" in pitch
    for words in ("product-dimension match rate", "contract key-bridge", "claim-to-contract coverage",
                  "fan-out guard", "claim sign", "no demo rows", "freshness", "posting-period",
                  "form-code coverage", "co-buyer feed join", "cancel reversals", "parent-contract links",
                  "agent of\n  record", "custom-field layer gap", "unassigned commission",
                  "filing-entity true zeros"):
        assert words in pitch, words
    arch = README.split("## Architecture")[1].split("## What it builds")[0]
    for source in ("CRM / call-center export", "Admin API extract"):
        assert source in arch, source
    assert "CRM call-center export and paginated admin API extract" in arch


def test_docs_do_not_contradict_obligor_conforming_or_read_as_a_bug_log():
    legacy = (ROOT / "docs" / "LEGACY_VS_BETTER.md").read_text(encoding="utf-8")
    sql = (ROOT / "dbt" / "models" / "intermediate" / "int_contracts_conformed.sql").read_text(encoding="utf-8")
    assert "obligor_basis" in sql
    limits = README.split("## Known limitations")[1]
    gaps = legacy.split("## What is still legacy-shaped")[1].split("##")[0]
    for text in (limits, gaps):
        assert "no obligor" not in text and "carry no obligor" not in text
    assert "obligor_basis" in legacy
    assert "Round 1" not in legacy and "internal review" not in legacy
    for phrase in ("instead of a person-name generator", "now prints", "no longer counts"):
        assert phrase not in legacy, phrase


# --------------------------------------------------------------------------- 7. environments match the code
def test_environment_docs_match_pipeline_envs():
    from pipeline import envs
    from pipeline.run import parse_args

    for name in envs.ENVS:
        assert parse_args(["--env", name]).env == name  # the documented flag really exists
    uat = envs.env_paths("uat", ROOT)
    scoping = README.split("## Scoping and environments")[1].split("## Quickstart")[0]
    quick = README.split("## Quickstart")[1].split("## Architecture")[0]
    for term in ("--env dev|uat|prod", "pipeline/envs.py", "tools/env_diff.py",
                 uat.warehouse.relative_to(ROOT).as_posix(), f"dbt/{uat.dbt_target}",
                 envs.env_paths("prod", ROOT).warehouse.relative_to(ROOT).as_posix()):
        assert term in scoping, term
    assert "pipeline.run --env uat" in quick and "env_diff.py --expect" in quick
    legacy = (ROOT / "docs" / "LEGACY_VS_BETTER.md").read_text(encoding="utf-8")
    bullet = legacy.split("- **Environments.**")[1].split("\n- **")[0]
    for term in ("--env dev|uat|prod", "pipeline/envs.py", "tools/env_diff.py", "data/env/<env>/warehouse.duckdb"):
        assert term in bullet, term
    release = (ROOT / "docs" / "operating-model" / "RELEASE.md").read_text(encoding="utf-8")
    sample = release.split("#### Sample promotion check")[1].split("\n## ")[0]
    assert re.search(r"env_diff uat vs prod: \d+ tables, \d+ pass, \d+ explained, \d+ unexplained", sample)
    assert "explained " in sample and "fail " in sample
    assert "DBT_TARGET_PATH=dbt/target_" not in release  # design 4.2: dbt resolves it under dbt/ itself


# --------------------------------------------------------------------------- 8. no V1 architecture in the details docs
DETAIL_DOCS = ("ARCHITECTURE.md", "DATA_MODEL.md")
RETIRED = re.compile(r"\b(?:bronze|silver|gold)\.|`(?:bronze|silver|gold)`|tpa_admin|"
                     r"\bstg_(?:contracts|claims|dealers|agents)\b")
MODEL_NAME = re.compile(r"^(?:stg|int|dim|fct|mart|snap)_[a-z0-9_]+$")


@pytest.mark.parametrize("doc", DETAIL_DOCS)
def test_detail_docs_describe_v3_not_v1(doc):
    text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
    hits = sorted({m.group(0) for m in RETIRED.finditer(text)})
    assert not hits, f"{doc} still names retired V1 objects: {hits}"
    for schema in ("raw_admin_core", "raw_legacy_admin", "raw_network_feed", "raw_crm", "raw_admin_api",
                   "staging", "intermediate", "snapshots", "`core`", "`finance`", "`claims`", "`dealer`",
                   "`regulatory`", "_source_version", "_is_deleted"):
        assert schema in text, (doc, schema)


@pytest.mark.parametrize("doc", DETAIL_DOCS)
def test_detail_docs_name_only_models_that_exist(doc):
    text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
    built = {p.stem for p in (ROOT / "dbt").rglob("*.sql")
             if not any(part.startswith(("target", "logs")) for part in p.relative_to(ROOT / "dbt").parts)}
    named = {tok.split(".")[-1] for tok in re.findall(r"`([a-z0-9_.]+)`", text)}
    missing = sorted(n for n in named if MODEL_NAME.match(n) and n not in built)
    assert not missing, f"{doc} names models that do not exist: {missing}"


def test_architecture_lists_every_source_step_and_recovery_command():
    from ingest.raw import RAW_SCHEMAS
    from pipeline.run import STEPS

    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for schema in RAW_SCHEMAS.values():
        assert f"`{schema}`" in text, schema
    for step in STEPS:
        assert f"| {step} |" in text, step
    for cmd in ("--from <step>", "--from ingest --full-refresh", "--sources legacy_admin --full-refresh",
                "--seed N", "--append-day", "--env dev|uat|prod"):
        assert cmd in text, cmd
    assert "docs/ARCHITECTURE.md" in README and "docs/DATA_MODEL.md" in README


# --------------------------------------------------------------------------- 9. visible output + roadmap
README_IMAGES = ("start-here.png", "department-hub.png", "experience.png", "legacy-vs-better.png",
                 "how-data-flows.png", "semantic-model.png")


def test_readme_shows_committed_screenshots_of_the_site():
    """site/ is git-ignored, so the README must embed committed screenshots a reader can see on the repo page."""
    assert "## See it without running it" in README
    pitch_end = README.index("## Legacy vs better")
    assert README.index("## See it without running it") < pitch_end
    for name in README_IMAGES:
        img = ROOT / "docs" / "img" / name
        assert f"](docs/img/{name})" in README[:pitch_end], name
        assert img.exists(), img
        head = img.read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n", name
        width = int.from_bytes(head[16:20], "big")
        assert width >= 800, (name, width)
        assert img.stat().st_size < 1_000_000, f"{name} too large to commit"
    from tools import screenshots

    assert {s[0] for s in screenshots.SHOTS} == set(README_IMAGES)
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert not any(line.strip().rstrip("/") in {"docs", "docs/img", "*.png"} for line in gitignore)


def test_screenshot_tool_dry_run_plans_every_shot(tmp_path, capsys):
    from tools import screenshots

    assert screenshots.main(["--site", str(tmp_path / "site"), "--out", str(tmp_path / "img"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    for _, page, _ in screenshots.SHOTS:
        assert page.split("/")[-1] in out
    assert screenshots.main(["--site", str(tmp_path / "missing"), "--out", str(tmp_path / "img")]) == 2


def _roadmap_rows() -> list[list[str]]:
    text = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    table = text.split("## Ranked backlog")[1].split("\n## ")[0]
    rows = [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in table.splitlines() if re.match(r"\|\s*\d+\s*\|", line)]
    return rows


def test_roadmap_is_ranked_with_value_risk_effort_and_owner():
    text = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    header = "| # | Item | Business metric affected | Value | Risk | Effort | Owner role | Status |"
    assert header in text
    rows = _roadmap_rows()
    assert [int(r[0]) for r in rows] == list(range(1, len(rows) + 1)) and len(rows) >= 6
    for r in rows:
        assert len(r) == 8, r
        assert r[3] in {"High", "Medium", "Low"}, r[:4]
        assert re.match(r"(High|Medium|Low)\b", r[4]), r[:5]
        assert re.match(r"(S|M|L)\b", r[5]), r[:6]
        assert r[6] and r[7], r[0]
    items = " ".join(r[1] for r in rows)
    for must in ("in-force book", "SLO", "promotion", "V1 pages", "late-fee", "combined-ratio"):
        assert must in items, must
    assert "in-force" in rows[0][1], "the not-migrated in-force book is the top item"
    assert "## One-page upward report" in text


def test_known_limitations_match_the_roadmap_top_items():
    limits = README.split("## Known limitations")[1]
    assert "docs/ROADMAP.md" in limits
    assert "not_migrated" in limits and "15,873" in limits
    assert "SLO" in limits and "env_diff" in limits
    legacy = (ROOT / "docs" / "LEGACY_VS_BETTER.md").read_text(encoding="utf-8")
    gaps = legacy.split("## What is still legacy-shaped")[1].split("##")[0]
    assert "ROADMAP.md" in gaps and "not_migrated" in gaps
    # the README must not promise an Operations-page env-diff section a fresh build does not have
    assert "saved to `data/env/env_diff.json` and shown on the Operations page" not in README


def test_roadmap_in_force_figures_match_the_migration_mart():
    """ROADMAP item 1 and the README quote the live not-migrated row; fail loudly once it is converted."""
    if not WAREHOUSE.exists():
        pytest.skip("no warehouse")
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        if not con.execute("select 1 from information_schema.tables where table_schema = 'core' "
                           "and table_name = 'mart_migration_parallel_run'").fetchone():
            pytest.skip("core.mart_migration_parallel_run not built")
        rows = con.execute("select status, legacy_rows, row_difference, amount_difference "
                           "from core.mart_migration_parallel_run "
                           "where entity = 'CONTRACT_IN_FORCE_AT_CUTOVER'").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    status, legacy_rows, row_diff, amount_diff = rows[0]
    assert status == "NOT_MIGRATED", "in-force book now migrates: update ROADMAP item 1 and README limitations"
    roadmap = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    quoted = f"{abs(int(row_diff)):,}"
    assert quoted in roadmap and quoted in README, quoted
    assert f"${abs(float(amount_diff)) / 1e6:.1f}M" in roadmap, amount_diff


# --------------------------------------------------------------------------- 10. showcase: hero, assets, case study
def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def test_readme_hero_strip_and_badges_match_showcase_stats():
    """The badges and "Results at a glance" are generated from docs/showcase_stats.json, never typed."""
    from tools import showcase_assets as sa

    stats = sa.load_stats()
    assert sa.render_readme(README, stats) == README, "run python tools/showcase_assets.py readme"
    hero = README.split("## See it without running it")[0]
    assert hero.index("docs/img/banner.png") < hero.index("# F&IP") < hero.index("## Results at a glance")
    for tid in sa.TILE_ORDER:
        assert f"**{stats['tiles'][tid]['value']}**" in hero, tid
    for badge in ("actions/workflows/ci.yml/badge.svg", "img.shields.io/badge/pytest-",
                  "img.shields.io/badge/dbt%20build-", "Python-3.12", "PostgreSQL-16", "Power%20BI-",
                  "license-MIT"):
        assert badge in hero, badge
    assert "docs/CASE_STUDY.md" in hero and "docs/img/walkthrough.gif" in hero
    assert "$6,187,028" not in README  # $6,187,027.54 is truncated, never rounded up


def test_showcase_images_exist_with_the_right_shape():
    img = ROOT / "docs" / "img"
    assert _png_size(img / "social-preview.png") == (1280, 640)
    w, h = _png_size(img / "banner.png")
    assert w >= 1200 and h < w
    gif = img / "walkthrough.gif"
    assert gif.read_bytes()[:6] in (b"GIF87a", b"GIF89a") and gif.stat().st_size < 8_000_000
    infographics = sorted({p.name.rsplit("-", 1)[0] for p in (img / "infographics").glob("0*-dark.png")})
    assert len(infographics) == 6, infographics
    for name in infographics:
        for theme in ("dark", "light"):
            assert f"docs/img/infographics/{name}-{theme}.png" in README, (name, theme)
            assert (img / "infographics" / f"{name}-{theme}.png").exists()


def test_case_study_is_generated_from_the_measured_numbers():
    from tools import showcase_assets as sa

    md = sa.case_study_md(sa.load_stats(), sa.load_bench())
    assert (ROOT / "docs" / "CASE_STUDY.md").read_text(encoding="utf-8") == md, \
        "run python tools/showcase_assets.py case-study"
    assert (ROOT / "docs" / "case_study.html").read_text(encoding="utf-8") == sa.case_study_html(md)
    for heading in ("## The problem", "## What I built", "## Measured results",
                    "## How I would run it at a real company",
                    "## Honest notes"):
        assert heading in md, heading
    assert "AI coding agents" in md and "I directed them" in md
    assert "synthetic" in md.lower()


def test_markdown_subset_renders_lists_and_tables():
    from tools.showcase_assets import md_to_html

    out = md_to_html("# T\n\n- **a** `b`\n1. c\n\n| x | y |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>T</h1>" in out and "<ul>\n<li><strong>a</strong> <code>b</code></li>\n</ul>" in out
    assert "<ol>" in out and "<th>x</th>" in out and "<td>2</td>" in out
