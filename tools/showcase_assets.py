"""Build the showcase assets around the README from the latest measured numbers.

Every number comes from ``docs/showcase_stats.json`` (written by ``python -m reports`` from the run
manifest, dbt results, audit tables, pytest record and legacy benchmark) or from the committed legacy
benchmark snapshot ``reports/legacy/benchmark_snapshot.json``. Nothing is typed by hand or rounded up.

    python tools/showcase_assets.py              # everything below
    python tools/showcase_assets.py readme       # README badges + "Results at a glance" strip only
    python tools/showcase_assets.py case-study   # docs/CASE_STUDY.md, docs/case_study.html (+ .pdf with a browser)
    python tools/showcase_assets.py images       # docs/img/banner.png, docs/img/social-preview.png
    python tools/showcase_assets.py walkthrough  # docs/img/walkthrough.gif from headless captures of site/

Images use a locally installed Chromium browser (Edge or Chrome) in headless mode, the same one
``tools/screenshots.py`` finds. They read local files only. All data shown is synthetic.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reports.showcase import budget, ms  # noqa: E402  (truncating formatters shared with the site tiles)
from tools.screenshots import find_browser  # noqa: E402

STATS = REPO_ROOT / "docs" / "showcase_stats.json"
BENCH = REPO_ROOT / "reports" / "legacy" / "benchmark_snapshot.json"
README = REPO_ROOT / "README.md"
IMG = REPO_ROOT / "docs" / "img"
CASE_MD = REPO_ROOT / "docs" / "CASE_STUDY.md"
CASE_HTML = REPO_ROOT / "docs" / "case_study.html"
CASE_PDF = REPO_ROOT / "docs" / "case_study.pdf"
SITE = REPO_ROOT / "site"
REPO_URL = "https://github.com/MichaelH117/fi-data-platform"

# The tile order of the README strip (ids from reports/showcase.py).
TILE_ORDER = ("proof_warehouse_rows", "proof_dbt_tests", "proof_pytest", "proof_rebuild_time",
              "proof_gates", "proof_legacy_wrong", "proof_legacy_speed", "proof_dealer_packets")

VALUE_LINE = ("One command turns the contracts dealers sell into reconciled, plain-English reports, and refuses "
              "to publish a number that does not tie out.")

BRAND, INK, BG, PANEL, TEXT, MUTED, CYAN = "#8ACE00", "#000000", "#0B0D0C", "#16191A", "#E8ECE4", "#9AA394", "#2EC4D6"
HEAD_FONT = "'Barlow Condensed','Bahnschrift','Arial Narrow',sans-serif"
MONO_FONT = "'JetBrains Mono',Consolas,monospace"
BODY_FONT = "Inter,'Segoe UI',Arial,sans-serif"


# --------------------------------------------------------------------------- inputs
def load_stats(path: Path = STATS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_bench(path: Path = BENCH) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {q["id"]: q for q in data["questions"]} | {"_generated_at": data.get("generated_at")}


def tile(stats: dict, tile_id: str) -> dict:
    return stats["tiles"][tile_id]


def num(v: float, digits: int = 0) -> str:
    """Exact figure with thousands separators; never rounds an integer-valued count."""
    return f"{v:,.{digits}f}"


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


# --------------------------------------------------------------------------- README
def _badge(label: str, message: str, color: str, link: str, alt: str) -> str:
    def esc(s: str) -> str:
        return quote(s.replace("-", "--").replace("_", "__"), safe="")
    url = f"https://img.shields.io/badge/{esc(label)}-{esc(message)}-{color}?labelColor=0B0D0C"
    return f"[![{alt}]({url})]({link})"


def badges(stats: dict) -> str:
    s = stats["stats"]
    py, dbt = s["pytest"], s["dbt"]
    py_msg = f"{py['passed']} passed" + (f", {py['failed']} failed" if py["failed"] else "")
    dbt_msg = f"{dbt['build_nodes_ok']} of {dbt['build_nodes_total']} passed" + \
        (f", {dbt['build_nodes_warned']} warn" if dbt["build_nodes_warned"] else "")
    green, amber = "8ACE00", "F2A93B"
    items = [
        f"[![CI]({REPO_URL}/actions/workflows/ci.yml/badge.svg)]({REPO_URL}/actions/workflows/ci.yml)",
        _badge("pytest", py_msg, amber if py["failed"] else green, "#checks", f"pytest: {py_msg}"),
        _badge("dbt build", dbt_msg, amber if dbt["build_nodes_failed"] else green, "#checks",
               f"dbt build: {dbt_msg}"),
        _badge("Python", "3.12+", "2EC4D6", "#quickstart", "Python 3.12+"),
        _badge("PostgreSQL", "16", "2EC4D6", "#real-power-bi-on-a-real-database", "PostgreSQL 16"),
        _badge("Power BI", "PBIP", "F2A93B", "powerbi/README.md", "Power BI project (PBIP)"),
        _badge("license", "MIT", "8ACE00", "LICENSE", "MIT license"),
    ]
    return "\n".join(items)


def results_strip(stats: dict) -> str:
    s = stats["stats"]
    rows = ["| Result | Value | What is behind it |", "|---|---|---|"]
    for tid in TILE_ORDER:
        t = tile(stats, tid)
        rows.append(f"| {t['label']} | **{t['value']}** | {t['sub']} |")
    rows.append("")
    finished = s["rebuild"]["finished_at"][:16].replace("T", " ")
    rows.append(f"Read from pipeline run `{s['run_id']}` (finished {finished} "
                f"UTC), its dbt results and audit tables, the pytest record of {s['pytest']['recorded_at'][:10]} and "
                f"the legacy benchmark of {s['legacy']['measured_at'][:10]}, by `python -m reports` "
                f"([docs/showcase_stats.json](docs/showcase_stats.json)). Short values are truncated, never rounded "
                "up. Synthetic data.")
    return "\n".join(rows)


def replace_block(text: str, name: str, body: str) -> str:
    pat = re.compile(rf"(<!-- {name}:start -->\n)(?:.*?\n)?(<!-- {name}:end -->)", re.S)
    if not pat.search(text):
        raise SystemExit(f"README is missing the <!-- {name}:start --> / <!-- {name}:end --> markers")
    return pat.sub(lambda m: m.group(1) + body + "\n" + m.group(2), text, count=1)


def render_readme(text: str, stats: dict) -> str:
    return replace_block(replace_block(text, "badges", badges(stats)), "results", results_strip(stats))


def write_lf(path: Path, text: str) -> Path:
    """Write with LF line endings on every OS, so a rebuild on Windows does not churn the diff."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\r\n", "\n"))
    return path


def write_readme(stats: dict) -> Path:
    return write_lf(README, render_readme(README.read_text(encoding="utf-8"), stats))


# --------------------------------------------------------------------------- case study
def case_study_md(stats: dict, bench: dict) -> str:
    s = stats["stats"]
    q01, q02, q03, q07, q10 = (bench[k] for k in ("q01", "q02", "q03", "q07", "q10"))
    q10_timeout = next(st.get("timeout") for st in q10.get("legacy_steps") or [q10["legacy"]] if st.get("timed_out"))
    leg = s["legacy"]
    lines = [
        "# Case study: F&IP, an F&I data platform",
        "",
        "Michael Hanson · demonstration project · all data synthetic · "
        f"numbers from pipeline run `{s['run_id']}`",
        "",
        "## The problem",
        "",
        "A company that administers the protection products dealers sell in the finance office (vehicle service "
        "contracts, GAP, maintenance plans) lives on its reports. Dealers, finance, compliance and executives "
        "each need numbers they can trust, every morning. The usual warehouse grew one urgent request at a time: "
        "reports read the admin system directly, the same dealer exists under three ids, one layer stores claims "
        "negative and another positive, and nobody can say whether a report still matches the source until "
        "someone complains.",
        "",
        "## What I built",
        "",
        "- **The whole chain, end to end.** A synthetic admin system, a daily dealer feed, CRM and admin-API "
        "intake with data contracts and quarantine, a layered dbt warehouse (raw, staging, intermediate, marts; "
        f"{num(s['dbt']['models_built'])} models built in the measured run), accounting-grade facts, state reserve "
        "filings, dealer statements and per-dealer packets.",
        "- **Gates that block publishing.** Sixteen diagnostics, a source-to-mart reconciliation and a publish "
        "gate. If one breaks, yesterday's reports stay up and nothing new goes out.",
        "- **Reports people can read.** A static report site with Excel downloads, nine department hubs, and "
        "a three-line plain-English explanation under every tile and chart.",
        "- **A Power BI path.** A semantic model generated from one metrics file, a Power BI project over a "
        "reconciled PostgreSQL copy of the marts, and dealer row-level security from one access map.",
        "- **The old way, rebuilt on purpose.** A legacy-style warehouse built from the same synthetic rows, "
        "so the same ten business questions can be measured both ways.",
        "",
        "## Measured results",
        "",
        "| Result | Value | What is behind it |",
        "|---|---|---|",
    ]
    for tid in TILE_ORDER:
        t = tile(stats, tid)
        lines.append(f"| {t['label']} | **{t['value']}** | {t['sub']} |")
    lines += [
        "",
        "Five of the ten legacy-vs-modern questions, same synthetic rows, same server "
        f"(benchmark of {leg['measured_at'][:10]}):",
        "",
        f"- One dealer's in-force book: legacy {num(q01['legacy']['answer'])} contracts, modern "
        f"{num(q01['modern']['answer'])} (the dealer exists under several ids).",
        f"- Contracts by product, latest plan year: legacy counted {num(q02['legacy']['answer'])}, modern "
        f"{num(q02['modern']['answer'])} (a stale product table, inner-joined).",
        f"- Paid claims: legacy {money(q03['legacy']['answer'])}, modern {money(q03['modern']['answer'])} (claim "
        "signs differ between layers).",
        f"- Provider-scoped lookup (the slow-query case): legacy {ms(q07['legacy']['ms'])}, modern "
        f"{ms(q07['modern']['ms'])} (a union of every layer is rebuilt for each lookup).",
        f"- Vehicle match by VIN: the legacy lookup timed out at {budget(q10_timeout)}; its fallback join matched "
        f"{num(q10['legacy']['answer'], 2)}%. Modern: {num(q10['modern']['answer'], 2)}% in "
        f"{ms(q10['modern']['ms'])} on an enforced key.",
        "",
        slower_line(bench),
        "",
        "## How I would run it at a real company",
        "",
        "1. **Weeks 1-2: listen and map.** Read-only access to the admin system, an inventory of every report and "
        "who uses it, and one owner per metric definition. No dashboards yet.",
        "2. **Land and reconcile first.** Copy sources untouched into raw, then tie row counts and dollars from "
        "source to raw to marts on every run. A report is only trusted once its numbers tie out.",
        "3. **Gates before go-live.** The checks that matter most (match rates, fan-out, claim sign, demo rows, "
        "freshness) block publishing; every run is logged against a 7 AM target.",
        "4. **Dev, UAT, prod.** Every change builds in UAT, is diffed against prod, and ships only when the "
        "change ticket explains every difference.",
        "5. **One definition, many audiences.** Metrics live in one file that generates the Power BI models; "
        "each department gets a hub with its questions, its pages and who to ask when a number looks wrong.",
        "6. **Then scale the platform.** The same dbt project already has Snowflake and Postgres targets, an "
        "Airflow DAG and row-level security scripts, written and ready for the company's accounts.",
        "",
        "## Honest notes",
        "",
        "- **Built with AI coding agents.** I directed them: I wrote the design contract, gave each agent one "
        "role on a data team's org chart, and checked every report it returned against the tests and run "
        "artifacts before accepting it. The agents wrote most of the code; I decided what shipped.",
        f"- The pytest record behind the table had {s['pytest']['failed']} failing of "
        f"{num(s['pytest']['collected'])} collected tests, shown as is.",
        "- The legacy in-force book at the 2023 cutover is not migrated; reconciliation reports it as a "
        "documented exclusion, not a pass. The Snowflake and Airflow pieces are written but not executed here.",
        "- All data is synthetic. No real company's data, names or layouts are used.",
        "",
        f"Code and README: {REPO_URL}",
        "",
    ]
    return "\n".join(lines)


def slower_line(bench: dict) -> str:
    slow = [k.upper() for k, q in sorted(bench.items())
            if not k.startswith("_") and q["modern"].get("ms") is not None and q["legacy"].get("ms") is not None
            and q["modern"]["ms"] > q["legacy"]["ms"]]
    if not slow:
        return "The modern side was faster on all ten questions."
    return (f"The modern side is slower on {len(slow)} of 10 questions ({' and '.join(slow)}); there the fix "
            "is the right answer, not speed.")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return re.sub(r"(https://\S+)", r'<a href="\1">\1</a>', out)


def md_to_html(md: str) -> str:
    """Just enough Markdown for the case study: headings, paragraphs, lists, one table."""
    out: list[str] = []
    lst: str | None = None
    table: list[list[str]] = []

    def close():
        nonlocal lst, table
        if lst:
            out.append(f"</{lst}>")
            lst = None
        if table:
            head, *body = [r for r in table if not set("".join(r)) <= set("-")]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            out.extend(["<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body])
            out.append("</tbody></table>")
            table = []

    for line in md.splitlines():
        if line.startswith("|"):
            table.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        m_ul, m_ol = re.match(r"- (.*)", line), re.match(r"\d+\. (.*)", line)
        if m_ul or m_ol:
            kind = "ul" if m_ul else "ol"
            if lst != kind:
                close()
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{_inline((m_ul or m_ol).group(1))}</li>")
            continue
        close()
        if line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    close()
    return "\n".join(out)


CASE_CSS = """
@page { size: Letter; margin: 0.38in 0.45in; }
:root { --brand:#8ACE00; --brand-ink:#3F6200; --ink-on-brand:#000; --text:#111410; --muted:#4A5245;
  --line:rgba(60,90,0,.30); --bg:#FFFFFF; }
* { box-sizing: border-box; }
html { background: var(--bg); }
body { margin: 0 auto; max-width: 7.5in; padding: 16px; color: var(--text); background: var(--bg);
  font: 8.2pt/1.3 Inter,'Segoe UI',Arial,sans-serif; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.mast { display:flex; align-items:center; gap:12px; border-bottom: 3px solid var(--brand); padding-bottom: 6px; }
.mark { background: var(--brand); color: var(--ink-on-brand);
  font: 800 20pt/1 'Barlow Condensed',Bahnschrift,sans-serif; padding: 5px 12px 4px;
  clip-path: polygon(0 0, calc(100% - 9px) 0, 100% 9px, 100% 100%, 0 100%); }
h1 { font: 700 17pt/1.1 'Barlow Condensed',Bahnschrift,sans-serif; margin: 0; }
h1 + p { margin: 2px 0 0; color: var(--muted); font-size: 8pt; }
h2 { font: 700 10.5pt/1.2 'Barlow Condensed',Bahnschrift,sans-serif; margin: 7px 0 2px; color: var(--brand-ink);
  text-transform: uppercase; letter-spacing: .04em; }
p { margin: 3px 0; }
ul, ol { margin: 2px 0; padding-left: 16px; }
li { margin: 1px 0; }
code { font: 7.8pt Consolas,'JetBrains Mono',monospace; }
strong { font-weight: 700; }
table { width: 100%; border-collapse: collapse; margin: 3px 0; font-size: 7.6pt; }
th { background: var(--brand); color: var(--ink-on-brand); text-align: left; padding: 3px 5px; font-weight: 700; }
td { border-bottom: 1px solid var(--line); padding: 2px 5px; vertical-align: top; }
td:first-child { white-space: nowrap; }
td:nth-child(2) { white-space: nowrap; font-family: Consolas,'JetBrains Mono',monospace; }
a { color: var(--brand-ink); }
.cols { columns: 2; column-gap: 22px; }
.cols h2 { break-after: avoid; } .cols li { break-inside: avoid; }
.cols h2:first-child { margin-top: 7px; }
.foot { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 4px; color: var(--muted); font-size: 7.5pt; }
@media screen { body { padding: 32px 16px; } }
@media print { body { padding: 0; } .foot { margin-top: 5px; } a { white-space: nowrap; } }
@media screen and (prefers-color-scheme: dark) {
  :root { --bg:#0B0D0C; --text:#E8ECE4; --muted:#9AA394; --brand-ink:#8ACE00; --line:rgba(138,206,0,.35); }
}
"""


def case_study_html(md: str) -> str:
    body = md_to_html(md)
    title, rest = body.split("\n", 1)
    run = "<h2>How I would run it"
    if run in rest:  # the closing sections sit side by side so the page prints on one sheet
        rest = rest.replace(run, '<div class="cols">' + run, 1) + "</div>"
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>F&amp;IP case study</title>"
            f"<style>{CASE_CSS}</style></head><body>\n"
            f"<header class=\"mast\"><div class=\"mark\">F&amp;IP</div><div>{title}\n"
            + rest.replace("</p>", "</p></div></header>", 1)
            + "\n<p class=\"foot\">Generated by tools/showcase_assets.py from docs/showcase_stats.json and "
            "reports/legacy/benchmark_snapshot.json. Synthetic data.</p>\n</body></html>\n")


def write_case_study(stats: dict, bench: dict, pdf: bool = True) -> list[Path]:
    md = case_study_md(stats, bench)
    write_lf(CASE_MD, md)
    write_lf(CASE_HTML, case_study_html(md))
    written = [CASE_MD, CASE_HTML]
    browser = find_browser() if pdf else None
    if browser:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={CASE_PDF}", CASE_HTML.resolve().as_uri()],
                       check=True, capture_output=True, timeout=120)
        written.append(CASE_PDF)
    return written


# --------------------------------------------------------------------------- banner + social preview
def _stat_cells(stats: dict) -> list[tuple[str, str]]:
    pick = ("proof_warehouse_rows", "proof_dbt_tests", "proof_gates", "proof_legacy_wrong", "proof_rebuild_time")
    return [(tile(stats, t)["value"], tile(stats, t)["label"]) for t in pick]


def _wordmark(size: int) -> str:
    """The F&IP block: one green rectangle, top-right corner cut, black letters, a cyan glitch slice."""
    w, h, cut = size * 2.5, size, size * 0.17
    fs = size * 0.62
    return f"""<svg width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" aria-hidden="true">
  <defs><clipPath id="sl"><rect x="0" y="{h * .55:.0f}" width="{w:.0f}"
    height="{max(3, h * .06):.0f}"/></clipPath></defs>
  <path d="M0 0H{w - cut:.0f}L{w:.0f} {cut:.0f}V{h:.0f}H0Z" fill="{BRAND}"/>
  <g font-family="{HEAD_FONT}" font-weight="800" font-size="{fs:.0f}" text-anchor="middle" letter-spacing="-1">
    <text x="{w / 2 - 2:.0f}" y="{h * .72:.0f}" fill="{CYAN}" clip-path="url(#sl)">F&amp;IP</text>
    <text x="{w / 2:.0f}" y="{h * .72:.0f}" fill="#000">F&amp;IP</text></g>
  <g fill="#000" fill-opacity=".12"><rect y="{h * .3:.0f}" width="{w:.0f}" height="1.5"/>
    <rect y="{h * .45:.0f}" width="{w:.0f}" height="1.5"/><rect y="{h * .84:.0f}" width="{w:.0f}" height="1.5"/></g>
</svg>"""


def _page(width: int, height: int, inner: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body {{ margin:0; width:{width}px; height:{height}px; overflow:hidden; background:{BG}; color:{TEXT}; }}
body {{ font-family:{BODY_FONT}; position:relative;
  background-image: linear-gradient(rgba(138,206,0,.06) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(138,206,0,.06) 1px, transparent 1px);
  background-size: 40px 40px; }}
.h {{ font-family:{HEAD_FONT}; font-weight:700; }}
.m {{ font-family:{MONO_FONT}; }}
.stats {{ display:flex; gap:14px; }}
.stat {{ flex:1; background:{PANEL}; border:1px solid rgba(138,206,0,.35); border-top:3px solid {BRAND};
  padding:14px 16px 12px; clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 0 100%); }}
.stat b {{ display:block; font-family:{MONO_FONT}; color:{BRAND}; font-size:30px; font-weight:700;
  white-space:nowrap; }}
.stat span {{ display:block; color:{MUTED}; font-family:{MONO_FONT}; font-size:13px; text-transform:uppercase;
  letter-spacing:.06em; margin-top:6px; }}
.tag {{ display:inline-block; background:{BRAND}; color:#000; font-family:{MONO_FONT}; font-size:14px;
  font-weight:700; padding:4px 10px; letter-spacing:.06em; }}
</style></head><body>{inner}</body></html>"""


def _cells(pairs: list[tuple[str, str]]) -> str:
    return "".join(f'<div class="stat"><b>{html.escape(v)}</b><span>{html.escape(lab)}</span></div>'
                   for v, lab in pairs)


def banner_html(stats: dict) -> str:
    cells = _cells(_stat_cells(stats)[:4])
    return _page(1280, 360, f"""
<div style="position:absolute;left:56px;top:48px">{_wordmark(104)}</div>
<div style="position:absolute;left:356px;top:50px;right:56px">
  <div class="h" style="font-size:52px;line-height:1">F&amp;I Data Platform</div>
  <div style="font-size:21px;line-height:1.35;margin-top:12px;max-width:820px">{html.escape(VALUE_LINE)}</div>
</div>
<div class="stats" style="position:absolute;left:56px;right:56px;top:206px">{cells}</div>
<div class="m" style="position:absolute;left:56px;bottom:18px;color:{MUTED};font-size:13px">
  Measured on pipeline run {html.escape(stats['stats']['run_id'])} · synthetic data</div>""")


def social_html(stats: dict) -> str:
    cells = _stat_cells(stats)
    top, bottom = _cells(cells[:3]), _cells(cells[3:])
    run_id = html.escape(stats["stats"]["run_id"])
    return _page(1280, 640, f"""
<div style="position:absolute;left:64px;top:60px">{_wordmark(120)}</div>
<div style="position:absolute;left:400px;top:64px;right:64px">
  <div class="h" style="font-size:64px;line-height:1">F&amp;I Data Platform</div>
  <div style="font-size:24px;line-height:1.35;margin-top:14px;max-width:780px">{html.escape(VALUE_LINE)}</div>
</div>
<div class="stats" style="position:absolute;left:64px;right:64px;top:262px">{top}</div>
<div class="stats" style="position:absolute;left:64px;right:64px;top:388px">{bottom}</div>
<div style="position:absolute;left:64px;right:64px;bottom:44px;display:flex;
  justify-content:space-between;align-items:center">
  <span class="tag">dbt · DuckDB · PostgreSQL · Power BI · Python</span>
  <span class="m" style="color:{MUTED};font-size:15px">run {run_id} · synthetic data</span>
</div>""")


def shoot(browser: str, page_html: str, out: Path, size: tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.html"
        write_lf(src, page_html)
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--virtual-time-budget=4000",
                        f"--window-size={size[0]},{size[1]}", f"--screenshot={out}",
                        src.as_uri()], check=True, capture_output=True, timeout=120)
    return out


def write_images(stats: dict, browser: str) -> list[Path]:
    return [shoot(browser, banner_html(stats), IMG / "banner.png", (1280, 360)),
            shoot(browser, social_html(stats), IMG / "social-preview.png", (1280, 640))]


# --------------------------------------------------------------------------- walkthrough GIF
# (page under site/, caption, plan). A plan is a list of ("hold", y, frames) / ("pan", y0, y1, frames) /
# ("zoom", (x0, y0, x1, y1), frames) steps over a 1400 px wide full-page capture. A zoom ends on the given box
# (the "Loss ratio, T12" KPI tile and its explain block on the Finance hub at 1400 px) and outlines it on the
# hold that follows. Re-check the box after a layout change: open docs/img/walkthrough.gif.
VIEW_W, VIEW_H = 1400, 875
CONTENT_X = 237  # first pixel right of the 236 px sidebar (--sidebar in reports/static/site.css)
OUT_W, OUT_H = 960, 600
STOPS = [
    ("start.html", "1 · Start here: pick your department", [("hold", 0, 22), ("pan", 0, 380, 12), ("hold", 380, 26)]),
    ("roles/finance.html", "2 · A department hub: its numbers, its questions, its pages",
     [("hold", 0, 22), ("pan", 0, 620, 12), ("hold", 620, 24)]),
    ("roles/finance.html", "3 · Every number explains itself in three plain lines",
     [("zoom", (549, 240, 810, 664), 10), ("hold", None, 44)]),
    ("legacy.html", "4 · Legacy vs better: ten questions, measured both ways",
     [("hold", 0, 18), ("pan", 0, 700, 14), ("hold", 700, 44)]),
]


def _capture(browser: str, page: str, out: Path, height: int = 3000) -> Path:
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--virtual-time-budget=8000", f"--window-size={VIEW_W},{height}", f"--screenshot={out}",
                    (SITE / page).resolve().as_uri()], check=True, capture_output=True, timeout=120)
    return out


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for name in (("bahnschrift.ttf",) if bold else ("segoeui.ttf",)) + ("arial.ttf", "DejaVuSans.ttf"):
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")):
            if (base / name).exists():
                return ImageFont.truetype(str(base / name), size)
    return ImageFont.load_default()


def _caption(frame, text: str):
    from PIL import ImageDraw

    d = ImageDraw.Draw(frame)
    d.rectangle([0, OUT_H - 44, OUT_W, OUT_H], fill=BRAND)
    d.text((18, OUT_H - 22), text, font=_font(22, bold=True), fill="#000000", anchor="lm")
    return frame


def _spotlight(img, box, pad: int = 10):
    """The page blurred and dimmed everywhere but ``box``: the zoom reads as a deliberate focus on one tile, and
    the neighbouring tiles cut at the frame edge become soft background instead of clipped text."""
    from PIL import Image, ImageFilter

    out = Image.blend(img.filter(ImageFilter.GaussianBlur(4)), Image.new("RGB", img.size, "#000000"), 0.6)
    keep = tuple(int(v) for v in (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
    out.paste(img.crop(keep), keep[:2])
    return out


def walkthrough_frames(browser: str, tmp: Path) -> list:
    from PIL import Image, ImageDraw

    frames = []
    shots: dict[str, Image.Image] = {}
    for page, caption, plan in STOPS:
        if page not in shots:
            shots[page] = Image.open(_capture(browser, page, tmp / (page.replace("/", "_") + ".png"))).convert("RGB")
        img = shots[page]
        box, highlight = (0, 0, VIEW_W, VIEW_H), None
        for step in plan:
            kind = step[0]
            if kind == "hold":
                if step[1] is not None:
                    box = (0, step[1], VIEW_W, step[1] + VIEW_H)
                boxes = [box] * step[2]
            elif kind == "pan":
                _, y0, y1, n = step
                boxes = [(0, y0 + (y1 - y0) * i / (n - 1), VIEW_W, y0 + (y1 - y0) * i / (n - 1) + VIEW_H)
                         for i in range(n)]
            else:  # zoom from the content column to the target box, padded to the viewport's aspect ratio
                (x0, y0, x1, y1), n = step[1], step[2]
                highlight = (x0, y0, x1, y1)
                h = (y1 - y0 + 40) * OUT_H / (OUT_H - 44)  # room for the caption bar
                w = h * VIEW_W / VIEW_H
                cx, top = (x0 + x1) / 2, y0 - 20
                left = max(cx - w / 2, CONTENT_X)
                target = (left, top, left + w, top + h)
                # Start on the main content right of the sidebar, so no frame of the zoom ever slices the
                # sidebar mid-word: both ends of every interpolated box sit at x >= CONTENT_X.
                start = box if box[0] >= CONTENT_X else \
                    (CONTENT_X, box[1], VIEW_W, box[1] + (VIEW_W - CONTENT_X) * VIEW_H / VIEW_W)
                boxes = [tuple(a + (b - a) * (i + 1) / n for a, b in zip(start, target, strict=True))
                         for i in range(n)]
            spot = _spotlight(img, highlight) if highlight else None
            for i, b in enumerate(boxes):
                frame = img.crop(tuple(int(v) for v in b)).resize((OUT_W, OUT_H), Image.LANCZOS)
                if spot is not None:  # ramp the spotlight in over the zoom, keep it on the hold that follows
                    t = (i + 1) / len(boxes) if kind == "zoom" else 1.0
                    frame = Image.blend(frame, spot.crop(tuple(int(v) for v in b)).resize((OUT_W, OUT_H),
                                                                                           Image.LANCZOS), t)
                frames.append(_caption(frame, caption))
            box = boxes[-1]
        if highlight and plan[-1][0] == "hold":  # outline the zoomed tile on the closing hold
            sx, sy = OUT_W / (box[2] - box[0]), OUT_H / (box[3] - box[1])
            rect = [(highlight[0] - box[0]) * sx - 6, (highlight[1] - box[1]) * sy - 6,
                    (highlight[2] - box[0]) * sx + 6, (highlight[3] - box[1]) * sy + 6]
            for f in frames[-plan[-1][2]:]:
                ImageDraw.Draw(f).rectangle(rect, outline=CYAN, width=4)
    return frames


def write_walkthrough(browser: str, out: Path = IMG / "walkthrough.gif") -> Path:
    from PIL import Image

    missing = [p for p, _, _ in STOPS if not (SITE / p).exists()]
    if missing:
        raise SystemExit(f"walkthrough: site pages missing ({', '.join(missing)}); run python -m reports first")
    with tempfile.TemporaryDirectory() as tmp:
        frames = walkthrough_frames(browser, Path(tmp))
    pal = [f.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    durations = [90] * len(pal)
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True, disposal=1)
    return out


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("what", nargs="*", default=["readme", "case-study", "images", "walkthrough"],
                        choices=["readme", "case-study", "images", "walkthrough"])
    parser.add_argument("--no-pdf", action="store_true", help="skip the PDF (no browser needed)")
    args = parser.parse_args(argv)
    stats = load_stats()
    needs_browser = {"images", "walkthrough"} & set(args.what)
    browser = find_browser()
    if needs_browser and not browser:
        print("showcase_assets: no Edge/Chrome found; set SCREENSHOT_BROWSER")
        return 2
    written: list[Path] = []
    if "readme" in args.what:
        written.append(write_readme(stats))
    if "case-study" in args.what:
        written += write_case_study(stats, load_bench(), pdf=not args.no_pdf)
    if "images" in args.what:
        written += write_images(stats, browser)
    if "walkthrough" in args.what:
        written.append(write_walkthrough(browser))
    for p in written:
        print(f"showcase_assets: wrote {p.relative_to(REPO_ROOT)} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
