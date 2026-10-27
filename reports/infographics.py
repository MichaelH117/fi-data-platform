"""Six textbook infographics (SVG, light and dark, plus 1600px PNGs) built from the latest run.

    python -m reports.infographics            # SVG + PNG into reports/static/infographics and docs/img/infographics
    python -m reports.infographics --no-png   # SVG only (no browser needed)

Every number is read, never typed: the warehouse (read-only), the latest run manifest and log in
``data/runs``, the dbt run_results copy, and the legacy benchmark JSON. ``numbers.json`` next to the SVGs
records each value and where it came from, and the site build (``reports/build.py``) copies the SVGs to
``site/assets/infographics``. PNGs are rendered by headless Edge/Chrome (``tools/screenshots.py``).
All data is synthetic.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT_DIRS = (ROOT / "reports" / "static" / "infographics", ROOT / "docs" / "img" / "infographics")
RUNS = ROOT / "data" / "runs"
WAREHOUSE = ROOT / "data" / "warehouse" / "warehouse.duckdb"
W = 1600
X0, X1 = 64, 1536  # content gutter

FIGS = {  # file stem -> (figure number, site caption)
    "01-contract-life": 1, "02-contract-dollar": 2, "03-earned-vs-written": 3,
    "04-dealer-participation": 4, "05-data-flow-gates": 5, "06-legacy-vs-better": 6,
}

THEMES = {
    "dark": dict(bg="#0B0D0C", panel="#16191A", panel2="#1D2122", text="#E8ECE4", text2="#C5CCBF",
                 muted="#9AA394", brand="#8ACE00", ink="#8ACE00", line="#3C5A12", soft="#2A302B",
                 grid="#232826", s1="#70A600", s2="#E0529C", s3="#2EC4D6", s4="#F2A93B", s5="#9A7BE0",
                 n1="#8C9488", n2="#5F665C", alert="#FF6B6B", tint="#1A2410"),
    "light": dict(bg="#F5F6F2", panel="#FFFFFF", panel2="#EEF0E9", text="#111410", text2="#343A31",
                  muted="#4A5245", brand="#8ACE00", ink="#3F6200", line="#B9C4A6", soft="#DADFD3",
                  grid="#E4E7DF", s1="#5C8B00", s2="#AC1B6E", s3="#00788A", s4="#A85A00", s5="#683EB6",
                  n1="#7A8276", n2="#A3AA9E", alert="#B02A2A", tint="#EEF5DD"),
}
HEAD = "'Barlow Condensed', Bahnschrift, 'Arial Narrow', sans-serif"
BODY = "Inter, 'Segoe UI', system-ui, sans-serif"
MONO = "'JetBrains Mono', Consolas, 'Courier New', monospace"


# --------------------------------------------------------------------------- formatting
def money(v: float, cents: bool = False) -> str:
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def num(v: float) -> str:
    return f"{v:,.0f}"


def pct(v: float, digits: int = 1) -> str:
    return f"{100 * v:.{digits}f}%"


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


# --------------------------------------------------------------------------- SVG primitives
def t(x, y, s, size=18, fill="#000", family=BODY, weight=400, anchor="start", extra="") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}"{extra}>{escape(str(s))}</text>')


def rect(x, y, w, h, fill, stroke="none", sw=1, extra="") -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{max(h, 0):.1f}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"{extra}/>')


def cut_panel(x, y, w, h, fill, stroke, cut=12, sw=1.5) -> str:
    """A panel with the brand's clipped top-right corner."""
    d = f"M{x},{y} H{x + w - cut} L{x + w},{y + cut} V{y + h} H{x} Z"
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def line(x1, y1, x2, y2, stroke, sw=2, dash="", arrow="") -> str:
    da = f' stroke-dasharray="{dash}"' if dash else ""
    me = f' marker-end="url(#{arrow})"' if arrow else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
            f'stroke-width="{sw}"{da}{me}/>')


def path(d, stroke, sw=2, fill="none", arrow="", dash="") -> str:
    me = f' marker-end="url(#{arrow})"' if arrow else ""
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{me}{da}/>'


def arrow_defs(c: dict) -> str:
    def m(name, color):
        return (f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" '
                f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
    return "<defs>" + m("ar", c["text2"]) + m("arg", c["ink"]) + m("arm", c["muted"]) + "</defs>"


def frame(c: dict, fig: int, h: int, title: str, takeaway: str, body: str, source: str, run_id: str,
          label: str) -> str:
    """Shared page: tag, title, one-sentence takeaway, body, source caption."""
    parts = [rect(0, 0, W, h, c["bg"]), arrow_defs(c),
             cut_panel(X0, 48, 178, 34, c["brand"], "none", cut=10, sw=0),
             t(X0 + 14, 71, f"F&IP · FIGURE {fig}", 16, "#000000", MONO, 700),
             t(X0, 138, title, 58, c["text"], HEAD, 800, extra=' letter-spacing="0.3"')]
    y = 182
    for ln in wrap(takeaway, 112):
        parts.append(t(X0, y, ln, 24, c["text2"], BODY, 400))
        y += 32
    parts.append(line(X0, y - 6, X1, y - 6, c["line"], 1))
    parts.append(body)
    src_lines = wrap("Source: " + source, 124)
    h = int(h + 22 * (len(src_lines) - 1))  # the footer grows downward; the body never moves
    parts[0] = rect(0, 0, W, h, c["bg"])
    fy = h - 30 - 22 * (len(src_lines) - 1)
    parts.append(line(X0, fy - 30, X1, fy - 30, c["soft"], 1))
    for i, ln in enumerate(src_lines):
        parts.append(t(X0, fy + 22 * i, ln, 15, c["muted"], MONO))
    stamp = run_id if run_id.startswith("benchmark") else f"run {run_id}"
    parts.append(t(X1, fy, f"Synthetic data · {stamp}", 15, c["muted"], MONO, anchor="end"))
    aria = escape(f"{title}. {takeaway}", {'"': "&quot;"})
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" '
            f'role="img" aria-label="{aria}"><title>{escape(title)}</title><desc>{escape(takeaway)} '
            f'{escape(label)}</desc>{"".join(parts)}</svg>')


# --------------------------------------------------------------------------- numbers
def latest_run() -> tuple[dict, Path]:
    manifests = sorted(p for p in RUNS.glob("2*.json"))
    ok = [p for p in manifests if json.loads(p.read_text(encoding="utf-8")).get("status") == "ok"]
    p = (ok or manifests)[-1]
    return json.loads(p.read_text(encoding="utf-8")), p


def collect() -> dict:
    """Every number the six figures show, with its source. Read-only."""
    import duckdb

    from legacy import report as legacy_report

    run, run_path = latest_run()
    run_id = run["run_id"]
    log = (RUNS / f"{run_id}.log").read_text(encoding="utf-8", errors="replace")
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    q = lambda sql, *a: con.execute(sql, list(a)).fetchall()  # noqa: E731
    n: dict = {"run_id": run_id, "run_seconds": run["seconds"], "finished_at": run["finished_at"]}

    # 1 life of a contract
    status = dict(q("select coalesce(status, 'NO_STATUS'), count(*) from core.dim_contract group by 1"))
    first, last, term = q("select min(entered_date), max(entered_date), "
                          "avg(datediff('month', effective_date, expiration_date)) from core.dim_contract")[0]
    claims, claim_contracts, paid, claim_days = q(
        "select count(*), count(distinct contract_key), sum(paid_amount), median(days_since_sale) "
        "from gold.fct_claim")[0]
    remit_n, remit_median, late = q("select count(*), median(remit_age_days), avg(is_late::int) "
                                    "from intermediate.int_remittance_lifecycle where remit_age_days is not null")[0]
    n["life"] = dict(status=status, contracts=sum(status.values()), first=str(first), last=str(last),
                     avg_term_months=term, claims=claims, claim_contracts=claim_contracts, claims_paid=paid,
                     claim_median_days=claim_days, remits=remit_n, remit_median_days=remit_median, remit_late=late,
                     source="core.dim_contract (status today), gold.fct_claim, "
                            "intermediate.int_remittance_lifecycle")

    # 2 where the dollar goes
    cols = ["dealer_markup", "reserve_amount", "commission", "admin_fee", "insurer_fee", "profit_share_reserve",
            "premium_tax"]
    row = q("select count(*), avg(retail_price), avg(coverage_cost), "
            + ", ".join(f"avg({k})" for k in cols)
            + ", max(abs(retail_price - dealer_markup - " + " - ".join(cols[1:]) + ")) from core.dim_contract_finance "
            "where " + " and ".join(f"{k} is not null" for k in cols))[0]
    all_n = q("select count(*) from core.dim_contract_finance")[0][0]
    n["dollar"] = dict(contracts=row[0], all_contracts=all_n, retail=row[1], coverage_cost=row[2],
                       parts=dict(zip(cols, row[3:3 + len(cols)], strict=True)), max_tie_gap=row[-1],
                       source=f"core.dim_contract_finance, average per contract over the {row[0]:,} of {all_n:,} "
                              "contracts whose seven-part split is complete (legacy-converted contracts carry fewer "
                              "parts); on every one, customer price = markup + the six remitted parts, to the cent")

    # 3 earned vs written
    earned = {m: float(v.replace(",", "")) for m, v in re.findall(
        r"actuarial INFO (\w+)\s+earned to valuation\s+([\d,]+)", log)}
    summ = dict(q("select metric, value from actuarial.run_summary where metric in "
                  "('valuation_date', 'upr_selected', 'earned_itd_selected_at_valuation', 'contracts')"))
    n["earning"] = dict(earned_by_method=earned, valuation_date=summ.get("valuation_date"),
                        upr_selected=float(summ["upr_selected"]),
                        earned_selected=float(summ["earned_itd_selected_at_valuation"]),
                        source="formulas in actuarial/earning.py (pro rata k/n; Rule of 78s k(2n-k+1)/(n(n+1))); "
                               f"book totals from the actuarial step of run {run_id} and actuarial.run_summary")

    # 4 participation
    closed = q("select max(posting_period_key) from finance.mart_cession_experience where is_closed")[0][0]
    groups = q("""select owns_claims_reserve, count(*), sum(itd_written_reserve), sum(itd_earned_reserve),
                         sum(itd_incurred_claims), sum(itd_ceding_fee), sum(itd_underwriting_result),
                         sum(itd_profit_share_due), sum(itd_distributions_paid), sum(closing_profit_share_due),
                         max(period_end)
                  from finance.mart_cession_experience where posting_period_key = ? group by 1""", closed)
    keys = ["entities", "written", "earned", "incurred", "ceding_fee", "uw", "ps_earned", "ps_paid", "ps_due",
            "period_end"]
    g = {("ceded" if r[0] else "kept"): dict(zip(keys, r[1:], strict=True)) for r in groups}
    tot = {k: sum(g[x][k] for x in g) for k in keys if k != "period_end"}
    tot["period_end"] = str(g["ceded"]["period_end"])
    for x in g.values():
        x["period_end"] = str(x["period_end"])
    terms = [dict(zip(["structure", "owns", "share", "fee", "season"], r, strict=True)) for r in q(
        "select structure_type, owns_claims_reserve, default_dealer_share_pct, ceding_fee_pct, min_seasoning_months "
        "from seeds.participation_structure_terms order by 1")]
    pc, pce = q("select sum(contracts), sum(contracts_ceded) from core.dim_participation_entity_program")[0]
    n["participation"] = dict(closed_period=closed, groups=g, total=tot, terms=terms, contracts=pc,
                              contracts_ceded=pce,
                              source=f"finance.mart_cession_experience at the latest closed period ({closed}), "
                                     "seeds.participation_structure_terms, core.dim_participation_entity_program")
    con.close()

    # 5 data flow gates (run log + dbt run_results)
    feed = re.search(r"processed (\d+) folder\(s\), (\d+) file\(s\).*?rejected files: (\d+), rejected rows: (\d+)",
                     log)
    ingest = re.search(r"ingest INFO loaded (\d+) rows, quarantined (\d+)", log)
    diag = re.search(r"diagnostics gate passed \((\d+) checks\)", log)
    calib = re.search(r"reconciliation calibration gate passed \((\d+) checks\)", log)
    ties = len(re.findall(r"pipeline\.reconcile .+ ok\s*$", log, flags=re.M))
    models = re.search(r"wrote (\d+) domain models, (\d+) measures", log)
    pages = re.search(r"reports built (\d+) pages", log)
    packets = re.search(r"dealer packets: (\d+), program packets: (\d+)", log)
    live = re.search(r"staged build (\S+) passed dbt, diagnostics and reconcile; it is now live", log)
    # The site can be rebuilt (python -m reports) after the run; its latest page count is recorded in
    # _site_build.json, used when it was built after this run started, so the figure matches the site.
    site_pages, pages_src = int(pages[1]), f"data/runs/{run_id}.log"
    site = RUNS / "_site_build.json"
    if site.exists():
        sb = json.loads(site.read_text(encoding="utf-8"))
        if str(sb.get("built_at", "")) >= str(run.get("started_at", ""))[:19].replace("+00:00", ""):
            site_pages, pages_src = int(sb["pages"]), f"data/runs/_site_build.json ({sb['built_at']})"
    rr = json.loads((RUNS / run_id / "dbt_build.json").read_text(encoding="utf-8"))
    dbt = Counter(f"{r['unique_id'].split('.')[0]}:{r['status']}" for r in rr["results"])
    n["flow"] = dict(feed_files=int(feed[2]), feed_rejected_files=int(feed[3]), feed_rejected_rows=int(feed[4]),
                     rows_loaded=int(ingest[1]), rows_quarantined=int(ingest[2]),
                     dbt_models=dbt["model:success"], dbt_tests_pass=dbt["test:pass"],
                     dbt_tests_warn=dbt["test:warn"], dbt_tests_fail=dbt["test:fail"] + dbt["test:error"],
                     dbt_unit_pass=dbt["unit_test:pass"], diag_checks=int(diag[1]), tie_outs=ties,
                     calib_checks=int(calib[1]), went_live=bool(live), pages=site_pages,
                     dealer_packets=int(packets[1]), program_packets=int(packets[2]),
                     semantic_measures=int(models[2]) if models else None,
                     source=f"data/runs/{run_id}.log and data/runs/{run_id}/dbt_build.json (dbt run_results); "
                            f"pages: {pages_src}")

    # 6 legacy vs better
    data = legacy_report.load()
    qs = data["questions"]
    rows = legacy_report.question_rows(data)
    timeouts = sum(1 for x in qs for s in (x.get("legacy_steps") or [x["legacy"]]) if s.get("timed_out"))
    faster = sum(1 for x in qs if x["modern"]["ms"] < x["legacy"]["ms"])
    failed = [x for x in qs if x["observed_failure"] not in ("none", "error")]
    n["legacy"] = dict(generated_at=data["generated_at"], rows=rows, questions=len(qs), failed=len(failed),
                       timeouts=timeouts, faster=faster,
                       slower=[x["id"].upper() for x in qs if x["modern"]["ms"] >= x["legacy"]["ms"]],
                       source=f"legacy benchmark measured {data['generated_at']} "
                              "(data/legacy/legacy_benchmark.json = reports/legacy/benchmark_snapshot.json), "
                              "same synthetic rows on one PostgreSQL 16 server")
    n["_manifest"] = str(run_path.relative_to(ROOT)).replace("\\", "/")
    return n


# --------------------------------------------------------------------------- figure 1
def fig_life(n: dict, c: dict) -> str:
    L = n["life"]
    st = L["status"]
    voided = st.get("VOIDED", 0) + st.get("DENIED", 0)
    other = L["contracts"] - st["INFORCE"] - st["EXPIRED"] - st["CANCELLED"]
    b = []
    nw, nh, y = 250, 168, 350
    xs = [X0, X0 + nw + 158, X0 + 2 * (nw + 158), X1 - nw]

    def node(x, y, kicker, title, value, sub, hi=False, color=None):
        fill = c["brand"] if hi else c["panel"]
        ink = "#000000" if hi else c["text"]
        sub_ink = "#000000" if hi else c["muted"]
        val_ink = "#000000" if hi else (color or c["ink"])
        out = [cut_panel(x, y, nw, nh, fill, c["brand"] if hi else c["line"]),
               t(x + 20, y + 32, kicker, 14, sub_ink, MONO, 700),
               t(x + 20, y + 68, title, 29, ink, HEAD, 800),
               t(x + 20, y + 116, value, 36, val_ink, MONO, 700)]
        for i, ln in enumerate(wrap(sub, 30)):
            out.append(t(x + 20, y + 146 + 20 * i, ln, 16, sub_ink, BODY))
        return "".join(out)

    b.append(node(xs[0], y, "STEP 1 · SOLD", "Dealer sells", num(L["contracts"]), "contracts written, all years"))
    b.append(node(xs[1], y, "STEP 2 · PENDING", "Pending", num(st.get("PENDING", 0)),
                  "sold, money not remitted yet"))
    b.append(node(xs[2], y, "STEP 3 · INFORCE", "In force", num(st["INFORCE"]), "covered today", hi=True))
    b.append(node(xs[3], y, "END · EXPIRED", "Expired", num(st["EXPIRED"]), "term ran out"))
    by = y + nh + 120
    b.append(node(xs[1], by, "END · VOIDED / DENIED", "Voided or denied", num(voided), "never remitted, or rejected",
                  color=c["muted"]))
    b.append(node(xs[2], by, "END · CANCELLED", "Cancelled", num(st["CANCELLED"]),
                  "refund of the unearned part", color=c["s2"]))
    mid = y + nh / 2

    def harrow(x1, x2, l1, l2):
        cx = (x1 + x2) / 2
        return (line(x1 + 4, mid, x2 - 6, mid, c["text2"], 2.5, arrow="ar")
                + t(cx, mid - 34, l1, 16, c["text"], BODY, 600, "middle")
                + t(cx, mid - 14, l2, 15, c["muted"], MONO, 400, "middle"))

    b.append(harrow(xs[0] + nw, xs[1], "booked", "status pending"))
    b.append(harrow(xs[1] + nw, xs[2], "dealer remits", f"median {L['remit_median_days']:.0f} days"))
    b.append(harrow(xs[2] + nw, xs[3], "term ends", f"avg {L['avg_term_months']:.1f} months"))

    def varrow(x, label):
        return (line(x + nw / 2, y + nh + 4, x + nw / 2, by - 6, c["text2"], 2.5, arrow="ar")
                + t(x + nw / 2 + 14, y + nh + 66, label, 16, c["text"], BODY, 600))

    b.append(varrow(xs[1], "voided"))
    b.append(varrow(xs[2], "customer cancels"))
    # claims loop above the in-force node
    lx1, lx2 = xs[2] + 70, xs[2] + nw - 70
    b.append(path(f"M{lx2},{y - 4} C{lx2},{y - 70} {lx1},{y - 70} {lx1},{y - 8}", c["ink"], 2.5, arrow="arg"))
    b.append(t((lx1 + lx2) / 2, y - 64, "claims", 16, c["ink"], BODY, 700, "middle"))
    cx = xs[2] + nw + 24
    b.append(t(cx, y - 84, "While covered, repairs are paid as claims", 17, c["text"], BODY, 600))
    b.append(t(cx, y - 60, f"{num(L['claims'])} claims on {num(L['claim_contracts'])} contracts", 16,
               c["text2"], MONO))
    b.append(t(cx, y - 38, f"{money(L['claims_paid'])} paid", 16, c["text2"], MONO))
    b.append(t(cx, y - 16, f"median {L['claim_median_days']:.0f} days after the sale", 16, c["text2"], MONO))

    # status bar
    sy = by + nh + 90
    b.append(t(X0, sy - 22, f"Where all {num(L['contracts'])} contracts stand today", 24, c["text"], HEAD, 800))
    segs = [("In force", st["INFORCE"], c["brand"], "#000000"), ("Expired", st["EXPIRED"], c["s3"], "#000000"),
            ("Cancelled", st["CANCELLED"], c["s2"], "#FFFFFF" if c is THEMES["light"] else "#000000"),
            ("Other", other, c["n1"], "#000000")]
    x, total, bw = X0, L["contracts"], X1 - X0
    lab_y = sy + 76
    for i, (name, v, col, _ink) in enumerate(segs):
        w = bw * v / total
        b.append(rect(x, sy, w - 2 if i < 3 else w, 40, col))
        if name != "Other":
            b.append(line(x + 1, sy + 40, x + 1, lab_y - 18, c["muted"], 1))
            b.append(t(x + 8, lab_y, f"{name} {num(v)} · {pct(v / total)}", 17, c["text"], MONO, 600))
        else:
            b.append(line(x + w - 1, sy + 40, x + w - 1, lab_y + 22, c["muted"], 1))
            b.append(t(x + w - 8, lab_y + 40,
                       f"Other {num(v)} · {pct(v / total)}: pending {num(st.get('PENDING', 0))}, "
                       f"voided or denied {num(voided)}, on hold or reinstating "
                       f"{num(other - st.get('PENDING', 0) - voided - st.get('NO_STATUS', 0))}"
                       + (f", no status {num(st['NO_STATUS'])}" if st.get("NO_STATUS") else ""),
                       15, c["muted"], MONO, anchor="end"))
        x += w
    h = sy + 260
    take = ("A contract is sold, waits for the dealer's money, is covered while claims are paid, and then "
            "either expires or is cancelled with a refund.")
    src = (f"{L['source']}. Contracts written {L['first']} to {L['last']}; remit median over {num(L['remits'])} "
           f"remittances ({pct(L['remit_late'])} late).")
    return frame(c, 1, h, "Life of a service contract", take, "".join(b), src, n["run_id"],
                 "State diagram with counts per status.")


# --------------------------------------------------------------------------- figure 2
PART_LABELS = {
    "dealer_markup": ("Dealer markup", "Dealer's price above its cost; the dealer keeps it"),
    "reserve_amount": ("Claims reserve", "Set aside to pay this contract's repair claims"),
    "commission": ("Commission", "Paid to the agent who signed up the dealer"),
    "admin_fee": ("Admin fee", "The administrator's fee for running the contract"),
    "insurer_fee": ("Insurer fee", "The insurer's fee for backing the reserve"),
    "profit_share_reserve": ("Profit-share reserve", "Held back for the dealer's future profit share"),
    "premium_tax": ("Premium tax", "State tax on the insurance part"),
}


def fig_dollar(n: dict, c: dict) -> str:
    D = n["dollar"]
    parts, retail = D["parts"], D["retail"]
    colors = {"dealer_markup": c["n1"], "reserve_amount": c["brand"], "commission": c["s2"], "admin_fee": c["s3"],
              "insurer_fee": c["s4"], "profit_share_reserve": c["s5"], "premium_tax": c["n2"]}
    b = []
    y0 = 262
    b.append(t(X0, y0 + 40, money(retail, True), 56, c["ink"], MONO, 700))
    b.append(t(X0 + 330, y0 + 18, "average price the customer pays", 20, c["text"], BODY, 600))
    b.append(t(X0 + 330, y0 + 44, f"over {num(D['contracts'])} synthetic contracts with a complete cost split", 18,
               c["muted"], BODY))
    by, bh = y0 + 150, 76
    bw = X1 - X0
    x = X0
    spans = {}
    for i, (k, v) in enumerate(parts.items()):
        w = bw * v / retail
        spans[k] = (x, w)
        b.append(rect(x, by, w - (2 if i < len(parts) - 1 else 0), bh, colors[k]))
        if w > 220:
            b.append(t(x + 16, by + 32, PART_LABELS[k][0], 20, "#000000", BODY, 700))
            b.append(t(x + 16, by + 60, f"{money(v, True)} · {pct(v / retail)}", 19, "#000000", MONO, 700))
        x += w
    # brackets: dealer keeps / dealer remits
    mx, mw = spans["dealer_markup"]
    rx = mx + mw
    for x1, x2, label in [(mx, rx - 4, f"Dealer keeps {money(parts['dealer_markup'], True)}"),
                          (rx, X1, f"Dealer remits the coverage cost {money(D['coverage_cost'], True)}")]:
        b.append(path(f"M{x1},{by - 12} V{by - 26} H{x2} V{by - 12}", c["text2"], 1.5))
        b.append(t((x1 + x2) / 2, by - 38, label, 19, c["text"], BODY, 700, "middle"))
    # small-segment callouts under the bar
    small = [k for k in parts if spans[k][1] <= 220]
    lx = rx + spans["reserve_amount"][1]
    for i, k in enumerate(small):
        sx, sw = spans[k]
        cxk = sx + sw / 2
        ty = by + bh + 30 + 22 * (i % 2)
        b.append(line(cxk, by + bh + 2, cxk, ty - 16, c["muted"], 1))
        b.append(t(cxk, ty, f"{i + 3}", 15, c["text2"], MONO, 700, "middle"))
    del lx
    # ranked table
    ty = by + bh + 108
    b.append(t(X0 + 52, ty, "PART", 14, c["muted"], MONO, 700))
    b.append(t(X0 + 360, ty, "WHAT IT PAYS FOR", 14, c["muted"], MONO, 700))
    b.append(t(1330, ty, "AVG PER CONTRACT", 14, c["muted"], MONO, 700, "end"))
    b.append(t(X1, ty, "SHARE", 14, c["muted"], MONO, 700, "end"))
    ty += 16
    for i, (k, v) in enumerate(parts.items()):
        ry = ty + 56 * i
        if i % 2 == 0:
            b.append(rect(X0, ry, bw, 56, c["panel"]))
        b.append(rect(X0 + 14, ry + 17, 22, 22, colors[k]))
        b.append(t(X0 + 52, ry + 35, f"{i + 1}. {PART_LABELS[k][0]}", 21, c["text"], BODY, 700))
        b.append(t(X0 + 360, ry + 35, PART_LABELS[k][1], 19, c["text2"], BODY))
        b.append(t(1330, ry + 36, money(v, True), 22, c["text"], MONO, 700, "end"))
        b.append(t(X1 - 12, ry + 36, pct(v / retail), 22, c["ink"] if k == "reserve_amount" else c["text"], MONO,
                   700, "end"))
    ry = ty + 56 * len(parts)
    b.append(line(X0, ry + 4, X1, ry + 4, c["text2"], 1.5))
    b.append(t(X0 + 52, ry + 38, "Customer price", 21, c["text"], BODY, 800))
    b.append(t(1330, ry + 38, money(retail, True), 22, c["text"], MONO, 700, "end"))
    b.append(t(X1 - 12, ry + 38, "100.0%", 22, c["text"], MONO, 700, "end"))
    h = int(ry + 170)
    res = parts["reserve_amount"]
    take = (f"Of the average {money(retail, True)} price, the dealer keeps {pct(parts['dealer_markup'] / retail)} as "
            f"markup and {pct(res / retail)} is set aside to pay claims; fees, commission and tax share the rest.")
    return frame(c, 2, h, "Where every dollar of a contract goes", take, "".join(b),
                 D["source"] + ". Shares are of the customer price.", n["run_id"], "100% stacked bar and table.")


# --------------------------------------------------------------------------- figure 3
def fig_earning(n: dict, c: dict) -> str:
    E = n["earning"]
    term = 60
    px0, px1, py0, py1 = 150, 1050, 330, 810  # plot box (x left/right, y top/bottom)
    sx = lambda k: px0 + (px1 - px0) * k / term  # noqa: E731
    sy = lambda f: py1 - (py1 - py0) * f  # noqa: E731
    pr = lambda k: k / term  # noqa: E731
    r78 = lambda k: k * (2 * term - k + 1) / (term * (term + 1))  # noqa: E731
    b = []
    for f in (0, .25, .5, .75, 1):
        b.append(line(px0, sy(f), px1, sy(f), c["grid"], 1))
        b.append(t(px0 - 14, sy(f) + 6, f"{int(f * 100)}%", 16, c["muted"], MONO, anchor="end"))
    for k in range(0, term + 1, 12):
        b.append(line(sx(k), py1, sx(k), py1 + 6, c["muted"], 1))
        b.append(t(sx(k), py1 + 28, f"{k}", 16, c["muted"], MONO, anchor="middle"))
    b.append(line(px0, py1, px1, py1, c["text2"], 1.5))
    b.append(t((px0 + px1) / 2, py1 + 60, "Months since the contract started (a 60-month contract)", 18, c["text2"],
               BODY, 600, "middle"))
    b.append(t(px0 - 70, py0 - 58, "Share of the premium counted as earned", 18, c["text2"], BODY, 600))
    ks = range(term + 1)
    area = (" ".join(f"{sx(k):.1f},{sy(r78(k)):.1f}" for k in ks) + " "
            + " ".join(f"{sx(k):.1f},{sy(pr(k)):.1f}" for k in reversed(ks)))
    b.append(f'<polygon points="{area}" fill="{c["s2"]}" fill-opacity="0.14"/>')
    b.append(line(px0, sy(1), px1, sy(1), c["text"], 2.5, dash="10 7"))
    b.append(t(px0 + 12, sy(1) - 14, "Written premium: 100% booked on the sale date", 17, c["text"], BODY, 700))
    b.append(f'<polyline points="{" ".join(f"{sx(k):.1f},{sy(pr(k)):.1f}" for k in ks)}" fill="none" '
             f'stroke="{c["s3"]}" stroke-width="3"/>')
    b.append(f'<polyline points="{" ".join(f"{sx(k):.1f},{sy(r78(k)):.1f}" for k in ks)}" fill="none" '
             f'stroke="{c["s2"]}" stroke-width="3"/>')
    # direct labels on the lines
    b.append(t(sx(40) + 16, sy(pr(40)) + 34, "Pro rata (even)", 19, c["text"], BODY, 700))
    b.append(rect(sx(40) - 2, sy(pr(40)) + 20, 12, 12, c["s3"]))
    b.append(rect(sx(3), sy(.64) - 12, 12, 12, c["s2"]))
    b.append(t(sx(3) + 20, sy(.64), "Rule of 78s (front-loaded)", 19, c["text"], BODY, 700))
    b.append(t(sx(43), sy((pr(43) + r78(43)) / 2) + 6, "earned sooner", 16, c["muted"], BODY, 600, "middle"))
    # marked points
    for k in (12, 30):
        for f, col in ((pr(k), c["s3"]), (r78(k), c["s2"])):
            b.append(f'<circle cx="{sx(k):.1f}" cy="{sy(f):.1f}" r="7" fill="{col}" stroke="{c["bg"]}" '
                     f'stroke-width="2.5"/>')
        b.append(line(sx(k), sy(r78(k)) + 8, sx(k), sy(pr(k)) - 8, c["muted"], 1, dash="3 4"))
        b.append(t(sx(k) + 14, sy(r78(k)) + 22, f"{pct(r78(k))}", 17, c["text"], MONO, 700))
        b.append(t(sx(k) + 14, sy(pr(k)) + 24, f"{pct(pr(k))}", 17, c["text"], MONO, 700))
    # side panel: book totals
    px, pw = 1130, X1 - 1130
    b.append(cut_panel(px, py0 - 20, pw, 540, c["panel"], c["line"]))
    b.append(t(px + 24, py0 + 16, f"THIS BOOK AT THE {E['valuation_date']} VALUATION", 14, c["muted"], MONO, 700))
    b.append(t(px + 24, py0 + 50, "Claims reserve earned to date if", 19, c["text"], BODY, 600))
    b.append(t(px + 24, py0 + 74, "every contract used one method", 19, c["text"], BODY, 600))
    ebm = E["earned_by_method"]
    items = [("Pro rata", ebm["pro_rata"], c["s3"]), ("Rule of 78s", ebm["rule_of_78s"], c["s2"]),
             ("Selected mix", ebm["selected"], c["brand"])]
    for i, (lab, v, col) in enumerate(items):
        yy = py0 + 124 + 74 * i
        b.append(rect(px + 24, yy - 16, 14, 14, col))
        b.append(t(px + 48, yy - 3, lab, 18, c["text2"], BODY, 600))
        b.append(t(px + 24, yy + 30, money(v), 30, c["text"], MONO, 700))
    diff = ebm["rule_of_78s"] - ebm["pro_rata"]
    yy = py0 + 124 + 74 * 3
    b.append(line(px + 24, yy - 22, px + pw - 24, yy - 22, c["soft"], 1))
    for i, ln in enumerate(wrap(f"Rule of 78s counts {money(diff)} more as earned than pro rata. "
                                "Earning too early overstates profit and understates the refund owed on a cancel.",
                                34)):
        b.append(t(px + 24, yy + 6 + 24 * i, ln, 17, c["text2"], BODY))
    b.append(t(px + 24, py0 + 492, f"Unearned (selected): {money(E['upr_selected'])}", 17, c["text"], MONO, 700))
    h = 1000
    k12 = 12
    take = (f"Written premium is booked on day one, but it is earned over the term: after {k12} of 60 months, "
            f"pro rata counts {pct(pr(k12))} as earned and the Rule of 78s counts {pct(r78(k12))}.")
    return frame(c, 3, h, "Earned vs written premium over a contract's life", take, "".join(b), E["source"] + ".",
                 n["run_id"], "Line chart of two earning curves with book totals.")


# --------------------------------------------------------------------------- figure 4
STRUCT = {"DEALER_OWNED_OBLIGOR": "Dealer-owned obligor", "CFC": "Controlled foreign reinsurer",
          "NCFC": "Non-controlled foreign reinsurer", "PORC": "Producer-owned reinsurer",
          "RETRO": "Retrospective", "PROFIT_SHARE": "Profit share only"}


def fig_participation(n: dict, c: dict) -> str:
    P = n["participation"]
    g, tot = P["groups"], P["total"]
    b = []
    y0 = 262
    # left: source node
    sx_, sw, sh = X0, 300, 190
    sy_ = y0 + 120
    b.append(cut_panel(sx_, sy_, sw, sh, c["panel"], c["line"]))
    b.append(t(sx_ + 20, sy_ + 34, "EVERY PARTICIPATING CONTRACT", 13, c["muted"], MONO, 700))
    b.append(t(sx_ + 20, sy_ + 72, "Claims reserve", 31, c["text"], HEAD, 800))
    b.append(t(sx_ + 20, sy_ + 116, money(tot["written"]), 30, c["ink"], MONO, 700))
    b.append(t(sx_ + 20, sy_ + 144, f"written to date, {tot['entities']} entities", 16, c["muted"], BODY))
    b.append(t(sx_ + 20, sy_ + 168, f"{num(P['contracts_ceded'])} of {num(P['contracts'])} contracts ceded", 16,
               c["muted"], BODY))
    lanes = [("ceded", "Reserve is ceded to the dealer's company", "The dealer carries the claims risk", c["brand"]),
             ("kept", "Reserve stays with the obligor", "The obligor carries the claims risk", c["s3"])]
    lx, lw, lh = 560, X1 - 560, 168
    for i, (key, head, risk, col) in enumerate(lanes):
        G = g[key]
        ly = y0 + 22 + i * (lh + 34)
        b.append(path(f"M{sx_ + sw + 4},{sy_ + sh / 2} C{sx_ + sw + 110},{sy_ + sh / 2} {lx - 120},{ly + lh / 2} "
                      f"{lx - 8},{ly + lh / 2}", col, 3, arrow="ar"))
        b.append(t(lx - 130, ly + lh / 2 - 14, "ceded" if key == "ceded" else "kept", 16, c["text"], MONO, 700,
                   "middle"))
        b.append(cut_panel(lx, ly, lw, lh, c["panel"], col, sw=2))
        b.append(rect(lx, ly, 8, lh, col))
        b.append(t(lx + 28, ly + 36, head, 27, c["text"], HEAD, 800))
        b.append(t(lx + 28, ly + 66, risk, 20, c["ink"] if key == "ceded" else c["text"], BODY, 700))
        structs = [s for s in P["terms"] if bool(s["owns"]) == (key == "ceded")]
        names = ", ".join(STRUCT.get(s["structure"], s["structure"]) for s in structs)
        for j, ln in enumerate(wrap(names, 92)):
            b.append(t(lx + 28, ly + 94 + 22 * j, ln, 16, c["muted"], BODY))
        shares = sorted({s["share"] for s in structs})
        fees = sorted({s["fee"] for s in structs})
        seas = sorted({s["season"] for s in structs})
        rule = (f"Default dealer share {' or '.join(pct(s, 0) for s in shares)} · ceding fee "
                f"{' to '.join(pct(f, 1) for f in (fees[0], fees[-1])) if len(fees) > 1 else pct(fees[0], 1)} "
                f"of earned · seasoning {' or '.join(str(s) for s in seas)} months")
        b.append(t(lx + 28, ly + 146, rule, 16, c["text2"], MONO))
        b.append(t(X1 - 20, ly + 36, f"{G['entities']} entities", 18, c["text"], MONO, 700, "end"))
        b.append(t(X1 - 20, ly + 64, f"{money(G['written'])} written", 18, c["text2"], MONO, 400, "end"))
    # waterfall: how the dealer's share is worked out
    wy = y0 + 2 * (lh + 34) + 70
    b.append(t(X0, wy, f"How the dealer's share is worked out: all {tot['entities']} entities, inception to "
               f"{tot['period_end']}", 24, c["text"], HEAD, 800))
    bars = [("Earned reserve", tot["earned"], "base", c["brand"]),
            ("less incurred claims", -tot["incurred"], "step", c["s2"]),
            ("less ceding fee", -tot["ceding_fee"], "step", c["s4"]),
            ("Underwriting result", tot["uw"], "base", c["text2"]),
            ("Profit share earned", tot["ps_earned"], "base", c["brand"]),
            ("already paid out", -tot["ps_paid"], "step", c["s3"]),
            ("Closing due to dealers", tot["ps_due"], "base", c["text2"])]
    vx0, vx1 = X0 + 300, 1290
    scale = (vx1 - vx0) / tot["earned"]
    run = 0.0
    for i, (lab, v, kind, col) in enumerate(bars):
        yy = wy + 30 + 52 * i
        if kind == "base":
            x1, x2 = vx0, vx0 + v * scale
            run = v
        else:
            x2, x1 = vx0 + run * scale, vx0 + (run + v) * scale
            run += v
        bold = 700 if kind == "base" else 400
        b.append(t(vx0 - 16, yy + 27, lab, 19, c["text"] if kind == "base" else c["text2"], BODY, bold, "end"))
        b.append(rect(x1, yy + 8, max(x2 - x1, 2), 28, col))
        if kind == "step" and i + 1 < len(bars):
            b.append(line(x1, yy + 36, x1, yy + 60, c["muted"], 1, dash="3 3"))
        b.append(t(max(x1, x2) + 12, yy + 29, ("- " if v < 0 else "") + money(abs(v)), 19, c["text"], MONO, 700))
        if i == 3:
            b.append(line(X0, yy + 48, X1, yy + 48, c["soft"], 1))
    note_y = wy + 30 + 52 * len(bars) + 30
    b.append(t(X0, note_y, "Underwriting result = earned reserve - incurred claims - ceding fee. Profit share = the "
               "dealer's share of a positive result, once seasoned.", 17, c["muted"], BODY))
    h = note_y + 140
    take = (f"In {g['ceded']['entities']} of {tot['entities']} participation deals the claims reserve is ceded and "
            "the dealer carries the risk; in the rest it stays with the obligor and the dealer only shares profit.")
    return frame(c, 4, h, "Dealer participation: who carries the risk", take, "".join(b), P["source"] + ".",
                 n["run_id"], "Two-lane flow and a waterfall.")


# --------------------------------------------------------------------------- figure 5
def fig_flow(n: dict, c: dict) -> str:
    F = n["flow"]
    b = []
    y0, sh = 276, 132
    cw = (X1 - X0 - 5 * 16) / 6
    g1 = X0 + cw + 8                      # gap centre over cards 1-2
    g2 = X0 + 4 * (cw + 16) - 8           # gap centre over cards 3-6
    stages = [(X0, g1 - 64, "Sources", "5 systems", "files and tables"),
              (g1 + 64, g1 + 304, "Raw warehouse", f"{num(F['rows_loaded'])} rows", "loaded this run, untouched"),
              (g1 + 424, g2 - 92, "Clean models", f"{F['dbt_models']} dbt models", "one shared shape"),
              (g2 + 92, X1, "Live reports", f"{F['pages']} pages",
               f"{F['dealer_packets']} dealer + {F['program_packets']} program packets")]
    for i, (x1, x2, name, l1, l2) in enumerate(stages):
        hi = i == 3
        b.append(cut_panel(x1, y0, x2 - x1, sh, c["brand"] if hi else c["panel"], c["brand"] if hi else c["line"]))
        ink = "#000000" if hi else c["text"]
        b.append(t(x1 + 18, y0 + 44, name, 30, ink, HEAD, 800))
        b.append(t(x1 + 18, y0 + 80, l1, 18, ink if hi else c["ink"], MONO, 700))
        b.append(t(x1 + 18, y0 + 106, l2, 16, "#000000" if hi else c["muted"], BODY))
    mid = y0 + sh / 2
    for i in range(3):
        b.append(line(stages[i][1] + 2, mid, stages[i + 1][0] - 6, mid, c["text2"], 2.5, arrow="ar"))
    b.append(t((stages[1][1] + stages[2][0]) / 2, mid - 14, "dbt builds", 15, c["muted"], MONO, 400, "middle"))

    def diamond(dx, k, s=17):
        return (f'<path d="M{dx},{mid - s} L{dx + s},{mid} L{dx},{mid + s} L{dx - s},{mid} Z" fill="{c["brand"]}" '
                f'stroke="{c["bg"]}" stroke-width="3"/>' + t(dx, mid + 6, str(k), 16, "#000000", MONO, 700, "middle"))

    b.append(diamond(g1 - 22, 1) + diamond(g1 + 22, 2))
    for k, dx in zip((3, 4, 5, 6), (g2 - 63, g2 - 21, g2 + 21, g2 + 63), strict=True):
        b.append(diamond(dx, k))
    cards = [
        ("File checks", "dealer network feed", [f"{F['feed_files']} files checked",
                                                f"{F['feed_rejected_files']} files rejected",
                                                f"{F['feed_rejected_rows']} rows rejected"], "bad file never lands"),
        ("Row quarantine", "every source", [f"{num(F['rows_loaded'])} loaded",
                                            f"{F['rows_quarantined']} quarantined"], "bad row set aside, kept"),
        ("Data tests", "dbt", [f"{F['dbt_tests_pass']} tests pass", f"{F['dbt_tests_warn']} warning",
                               f"{F['dbt_tests_fail']} failures", f"{F['dbt_unit_pass']} unit tests pass"],
         "build stops"),
        ("Diagnostics", "business rules", [f"{F['diag_checks']} checks pass"], "build stops"),
        ("Reconciliation", "source = warehouse", [f"{F['tie_outs']} tie-outs ok", "rows and dollars",
                                                  f"{F['calib_checks']} calibration checks"], "build stops"),
        ("Publish gate", "write, audit, publish", ["went live" if F["went_live"] else "held back",
                                                   "only after 3, 4, 5", "all passed"],
         "yesterday's reports stay up"),
    ]
    cy = y0 + sh + 130
    by = cy - 44
    # brackets: one stem from each gate group down to its cards
    for gx, first, last in ((g1, 0, 1), (g2, 2, 5)):
        c1 = X0 + first * (cw + 16) + cw / 2
        c2 = X0 + last * (cw + 16) + cw / 2
        b.append(line(gx, mid + 22, gx, by, c["muted"], 1.5))
        b.append(line(c1, by, c2, by, c["muted"], 1.5))
        for i in range(first, last + 1):
            cx_ = X0 + i * (cw + 16) + cw / 2
            b.append(line(cx_, by, cx_, cy - 6, c["muted"], 1.5, arrow="arm"))
    b.append(t(g1 + 12, by - 14, "on the way in", 15, c["muted"], MONO))
    b.append(t(g2 + 12, by - 14, "before anything is published", 15, c["muted"], MONO))
    for i, (title, where, lines_, fail) in enumerate(cards):
        cx = X0 + i * (cw + 16)
        b.append(cut_panel(cx, cy, cw, 300, c["panel"], c["line"]))
        b.append(rect(cx, cy, cw - 10, 6, c["brand"]))
        b.append(t(cx + 16, cy + 44, f"{i + 1} · {title}", 24, c["text"], HEAD, 800))
        b.append(t(cx + 16, cy + 70, where, 15, c["muted"], MONO))
        for j, ln in enumerate(lines_):
            b.append(t(cx + 16, cy + 108 + 27 * j, ln, 17, c["text"], MONO, 700 if j == 0 else 400))
        b.append(line(cx + 16, cy + 228, cx + cw - 16, cy + 228, c["soft"], 1))
        b.append(t(cx + 16, cy + 254, "IF IT FAILS", 13, c["muted"], MONO, 700))
        b.append(t(cx + 16, cy + 280, fail, 16, c["text2"], BODY, 600))
    h = int(cy + 300 + 130)
    take = ("Data passes six quality gates on its way from the source systems to a report; if gates 3 to 5 fail, "
            "nothing is published and yesterday's reports stay up.")
    return frame(c, 5, h, "How the data flows, and where it is checked", take, "".join(b),
                 F["source"] + f"; end-to-end run {n['run_seconds']} s.", n["run_id"],
                 "Pipeline with six numbered gates.")


# --------------------------------------------------------------------------- figure 6
def fig_legacy(n: dict, c: dict) -> str:
    L = n["legacy"]
    b = []
    y0 = 262
    tiles = [(f"{L['failed']} of {L['questions']}", "legacy answers were wrong, slow or needed a manual step",
              c["alert"]),
             (f"{L['timeouts']}", "legacy query hit its 60 s timeout", c["alert"]),
             (f"{L['faster']} of {L['questions']}", "questions answered faster on the modern schema", c["ink"])]
    tw = (X1 - X0 - 2 * 24) / 3
    for i, (val, lab, col) in enumerate(tiles):
        x = X0 + i * (tw + 24)
        b.append(cut_panel(x, y0, tw, 120, c["panel"], c["line"]))
        b.append(t(x + 22, y0 + 60, val, 46, col, MONO, 700))
        for j, ln in enumerate(wrap(lab, 44)):
            b.append(t(x + 22, y0 + 90 + 20 * j, ln, 16, c["text2"], BODY))
    ty = y0 + 180
    cols = [(X0, "Q", "start"), (X0 + 60, "BUSINESS QUESTION", "start"), (640, "WHAT WENT WRONG", "start"),
            (900, "LEGACY", "end"), (1060, "MODERN", "end"), (1340, "LEGACY TIME", "end"), (X1, "MODERN TIME", "end")]
    for x, lab, a in cols:
        b.append(t(x, ty, lab, 14, c["muted"], MONO, 700, a))
    b.append(t(980, ty - 24, "ANSWER", 14, c["muted"], MONO, 700, "middle"))
    b.append(line(830, ty - 18, 1060, ty - 18, c["soft"], 1))
    rh = 66
    ty += 14
    for i, r in enumerate(L["rows"]):
        q, question, headline, leg, mod, lt, mt, fail = r
        ry = ty + rh * i
        if i % 2 == 0:
            b.append(rect(X0 - 8, ry, X1 - X0 + 16, rh, c["panel"]))
        b.append(t(X0, ry + 40, q, 18, c["text2"], MONO, 700))
        b.append(t(X0 + 60, ry + 30, question, 19, c["text"], BODY, 600))
        b.append(t(X0 + 60, ry + 54, headline, 15, c["muted"], MONO))
        fw = 44 + 9.4 * len(fail)
        b.append(f'<path d="M640,{ry + 18} H{640 + fw - 8} L{640 + fw},{ry + 26} V{ry + 48} H640 Z" fill="none" '
                 f'stroke="{c["alert"]}" stroke-width="1.5"/>')
        b.append(t(648, ry + 40, f"✕ {fail}", 16, c["alert"], MONO, 700))
        b.append(t(900, ry + 40, leg, 18, c["text2"], MONO, 400, "end"))
        b.append(t(1060, ry + 40, mod, 18, c["text"], MONO, 700, "end"))
        b.append(t(1340, ry + 40, lt, 17, c["text2"], MONO, 400, "end"))
        slower_row = q in L["slower"]
        b.append(t(X1, ry + 40, mt + (" (slower)" if slower_row else ""), 18,
                   c["text2"] if slower_row else c["ink"], MONO, 700, "end"))
    ny = ty + rh * len(L["rows"]) + 36
    slower = " and ".join(L["slower"])
    b.append(t(X0, ny, f"Modern is slower on {slower}: there the fix is the right answer, not speed. Same synthetic "
               "rows on both sides, so every difference comes from structure.", 17, c["muted"], BODY))
    h = ny + 120
    take = (f"Asked the same {L['questions']} business questions, the old warehouse got every one wrong, slow or "
            "manual; the modern star schema answers them directly from one consistent model.")
    return frame(c, 6, h, "Legacy vs better at a glance", take, "".join(b), L["source"] + ".",
                 f"benchmark {L['generated_at']}",
                 "Benchmark table.")


BUILDERS = {"01-contract-life": fig_life, "02-contract-dollar": fig_dollar, "03-earned-vs-written": fig_earning,
            "04-dealer-participation": fig_participation, "05-data-flow-gates": fig_flow,
            "06-legacy-vs-better": fig_legacy}


# --------------------------------------------------------------------------- site embedding
def copy_to_site(assets: Path, src: Path = OUT_DIRS[0]) -> list[Path]:
    """Copy the committed SVGs into ``site/assets/infographics`` (the build reads no warehouse for them)."""
    out = assets / "infographics"
    files = sorted(src.glob("*.svg")) if src.exists() else []
    if files:
        out.mkdir(parents=True, exist_ok=True)
    for f in files:
        (out / f.name).write_bytes(f.read_bytes())
    return [out / f.name for f in files]


def figure(stem: str, page_path: str, caption: str, src: Path = OUT_DIRS[0]) -> str:
    """Trusted markup for one figure: dark and light <img> (CSS shows the one for the theme), alt text = the
    SVG's own title and takeaway, and a short plain-English caption. Empty when the SVG was never built."""
    dark = src / f"{stem}-dark.svg"
    if not dark.exists():
        return ""
    alt = re.search(r'aria-label="([^"]*)"', dark.read_text(encoding="utf-8"))
    alt = alt[1] if alt else stem  # already XML-escaped, which is valid HTML attribute text
    root = "../" * page_path.count("/")
    base = f"{root}assets/infographics/{stem}"
    h = svg_height(dark.read_text(encoding="utf-8"))
    return ('<figure class="infographic">'
            + "".join(f'<a class="ig-{th}" href="{base}-{th}.svg"><img src="{base}-{th}.svg" alt="{alt}" '
                      f'loading="lazy" width="{W}" height="{h}"></a>' for th in ("dark", "light"))
            + f'<figcaption>{escape(caption)} <span class="ig-open">Click the figure to open it full size.</span>'
            f'</figcaption></figure>')


# --------------------------------------------------------------------------- output
def svg_height(svg: str) -> int:
    return int(re.search(r'height="(\d+)"', svg)[1])


def to_png(svg_path: Path, png_path: Path, h: int, browser: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "p.html"
        page.write_text(f'<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;padding:0;'
                        f'overflow:hidden}}img{{display:block}}</style></head><body><img src="{svg_path.as_uri()}" '
                        f'width="{W}" height="{h}"></body></html>', encoding="utf-8")
        shot = Path(tmp) / "s.png"
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--allow-file-access-from-files",
                        f"--user-data-dir={tmp}/prof", "--virtual-time-budget=3000",
                        f"--window-size={W},{h}", f"--screenshot={shot}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
        png_path.write_bytes(shot.read_bytes())


def default(o):
    return str(o)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--only", help="one file stem, e.g. 03-earned-vs-written")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    n = collect()
    browser = None
    if not args.no_png:
        from tools.screenshots import find_browser
        browser = find_browser()
        if not browser:
            print("infographics: no Edge/Chrome found; writing SVG only")
    for d in OUT_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        (d / "numbers.json").write_text(json.dumps(n, indent=2, default=default), encoding="utf-8")
    for stem, fn in BUILDERS.items():
        if args.only and stem != args.only:
            continue
        for theme, c in THEMES.items():
            svg = fn(n, c)
            for d in OUT_DIRS:
                p = d / f"{stem}-{theme}.svg"
                p.write_text(svg, encoding="utf-8")
            if browser:
                src = OUT_DIRS[0] / f"{stem}-{theme}.svg"
                png = OUT_DIRS[1] / f"{stem}-{theme}.png"
                to_png(src, png, svg_height(svg), browser)
                (OUT_DIRS[0] / png.name).write_bytes(png.read_bytes())
            print(f"infographics: {stem}-{theme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
