"""Platform > Legacy vs better (``legacy.html``): the measured legacy pain points and their fixes.

A report plug-in like any area package: ``build(con, out_dir) -> list[Page]``. It reads no warehouse
tables. The numbers come from the last ``python -m legacy demo`` run (``data/legacy/legacy_benchmark.json``),
or from the committed snapshot next to this file when no local run exists (CI has no PostgreSQL), and the
page says when they were measured. The catalog (what it was, root cause, fix, where) lives in
``legacy/report.py`` so this page and ``docs/LEGACY_BENCHMARK.md`` never disagree.
"""

from __future__ import annotations

from pathlib import Path

from markupsafe import escape

from reports.build import Kpi, Page, Section

NAV_LABEL = "Legacy vs better"
NAV_GROUP = "Platform"
PATH = "legacy.html"


def _card(e: dict) -> str:
    def row(label: str, text: str) -> str:
        return f"<dt>{escape(label)}</dt><dd>{escape(text)}</dd>"

    measured = (", ".join(q.upper() for q in e["questions"]) or e.get("measured_by")
                or "schema-registry check")
    return ("<dl class=\"pain-point\">" + row("What it was", e["what"]) + row("Measured impact", e["impact"])
            + row("Root cause in the foundation", e["root_cause"]) + row("The fix", e["fix"])
            + row("Where it lives", e["where"]) + row("Measured by", measured) + "</dl>")


def build(con, out_dir: Path) -> list[Page]:
    from legacy import benchmark, report

    data = report.load()
    if data is None:
        return [Page(PATH, "Legacy vs better", "No legacy benchmark has been measured yet. Run "
                     "python -m legacy demo against a local PostgreSQL server to fill this page.")]
    if report.STATE_JSON.exists():
        benchmark.copy_to_site(out_dir, report.STATE_JSON)
    entries = report.entries(data)
    qs = data.get("questions", [])
    failed = [q for q in qs if q["observed_failure"] not in ("none", "error")]
    timeouts = sum(1 for q in qs for s in (q.get("legacy_steps") or [q["legacy"]]) if s.get("timed_out"))
    kpis = [Kpi("Questions answered both ways", str(len(qs)), f"measured {data.get('generated_at', '?')}"),
            Kpi("Legacy answers that were wrong or failed", f"{len(failed)} of {len(qs)}",
                "lost, doubled, zeroed, sign, stale, skewed or an extra manual step",
                "critical" if failed else "good"),
            Kpi("Legacy queries that hit their timeout", str(timeouts), "cancelled at the budget, never left running",
                "warning" if timeouts else ""),
            Kpi("Pain points fixed in the foundation", str(len(entries)), "each with its fix and where it lives")]
    sections = [
        Section("The benchmark", "Each business question answered on the legacy database the way the old "
                "warehouse forced it, and on the modern star schema. Same server, same synthetic raw rows.",
                cols=["Q", "Business question", "Headline", "Legacy", "Modern", "Legacy time", "Modern time",
                      "Failure"],
                rows=report.question_rows(data), wide=True, table_open=True,
                note="Plans: EXPLAIN ANALYZE when a query finished quickly, plain EXPLAIN when it ran long or "
                     "timed out. Full detail: docs/LEGACY_BENCHMARK.md. SQL: sql/playbook/benchmark/."),
    ]
    sections += [Section(f"{e['num']}. {e['title']}", "", html=_card(e), wide=True) for e in entries]
    return [Page(PATH, "Legacy vs better",
                 "How the old warehouse answered ten business questions, what it got wrong, the root cause in "
                 "its foundation, and the fix in this platform. All data is synthetic.", kpis, sections)]
