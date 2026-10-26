"""The ``F&IP`` (F&I Data Platform) report in PBIR format (one JSON file per page and per visual), generated.

Pages and visuals are declared here, on one layout grid, and written as PBIR JSON. PBIR is the format
Power BI Desktop saves a Power BI project (PBIP) in by default; it needs no preview feature in the
release this was built with (2.159). Every field a visual uses is checked against the semantic model
by ``powerbi.validate``.

Grid: 1280 x 848 page, 24 px margin, 12 columns of 88 px with 16 px gutters. Rows: header (wordmark image +
title), a plain-English "what this page shows / why your boss asks" card, filters, KPI cards (each with its
meaning in words, visible without hovering), then two chart rows.

Look (docs/V3_DESIGN.md 10.1): the F&IP dark tokens from ``reports/static/site.css``. Brand green is used for
borders and accents only; nothing puts light text on a green fill (a green fill always carries black text).
"""

from __future__ import annotations

import csv
import functools
import hashlib
import json
from pathlib import Path

from powerbi import model

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/"
S_VISUAL = SCHEMA + "item/report/definition/visualContainer/2.13.0/schema.json"
S_PAGE = SCHEMA + "item/report/definition/page/2.1.0/schema.json"
S_PAGES = SCHEMA + "item/report/definition/pagesMetadata/1.1.0/schema.json"
S_REPORT = SCHEMA + "item/report/definition/report/3.3.0/schema.json"
S_VERSION = SCHEMA + "item/report/definition/versionMetadata/1.0.0/schema.json"
S_PBIR = SCHEMA + "item/report/definitionProperties/2.0.0/schema.json"
S_PBIP = SCHEMA + "pbip/pbipProperties/1.0.0/schema.json"
S_PBISM = SCHEMA + "item/semanticModel/definitionProperties/1.0.0/schema.json"

W, H, M, G, COLS = 1280, 848, 24, 16, 12
COL_W = (W - 2 * M - (COLS - 1) * G) // COLS  # 88
ROW_HEADER, ROW_EXPLAIN, ROW_FILTERS = (8, 52), (68, 84), (160, 56)
ROW_KPI, ROW_A, ROW_B = (224, 132), (364, 226), (598, 226)

# F&IP dark tokens (reports/static/site.css :root). Power BI themes take solid hex only, so the site's
# translucent tokens are pre-blended over the panel: --line (35% brand), --grid (7% text), status tints (18%).
INK, INK_2, INK_3 = "#E8ECE4", "#C5CCBF", "#9AA394"  # --text, --text-2, --muted
PAGE_BG, SURFACE, SURFACE_2 = "#0B0D0C", "#16191A", "#1D2122"  # --bg, --panel, --panel-2
BORDER, GRID = "#3F5811", "#252828"
BRAND, INK_ON_BRAND = "#8ACE00", "#000000"  # a green fill always carries black text
ACCENT = BRAND
# Chart slots 1-8 (site.css --s1..--s8, dark), validated by the Front-end Developer with the dataviz palette
# validator: slot 1 is the brand hue one step darker; slot 5 is violet (red is kept for "Needs action").
SERIES = ["#70A600", "#C72481", "#00A7AE", "#D57700", "#7F57D1", "#8C9488", "#5F665C", "#B6BDB0"]
GOOD, WARNING, CRITICAL = "#8ACE00", "#F2A93B", "#FF6B6B"  # --good, --watch, --alert
CRITICAL_SOFT, WARNING_SOFT, GOOD_SOFT = "#402829", "#3E3320", "#2F4214"
FONT_HEAD, FONT_BODY, FONT_NUM = "Bahnschrift SemiBold", "Segoe UI", "Bahnschrift"  # system fallbacks (10.1)
WORDMARK = "fip-wordmark.svg"  # reports/static/fip-wordmark.svg, shipped as a registered image
WORDMARK_SRC = Path(__file__).resolve().parents[1] / "reports" / "static" / WORDMARK
EXPLANATIONS = Path(__file__).resolve().parents[1] / "dbt" / "seeds" / "core" / "metric_explanations.csv"

MEASURES = "Key Measures"
TI = ("Time Intelligence", "Time Calculation")
PERIOD = "Posting Period"


# ---------------------------------------------------------------------- query helpers
def _src(entity: str) -> dict:
    return {"SourceRef": {"Entity": entity}}


def measure(prop: str, entity: str = MEASURES) -> dict:
    return {"Measure": {"Expression": _src(entity), "Property": prop}}


def column(entity: str, prop: str) -> dict:
    return {"Column": {"Expression": _src(entity), "Property": prop}}


def field_ref(f: dict) -> tuple[str, str, str]:
    """(kind, entity, property) of a field expression."""
    kind = next(iter(f))
    return kind, f[kind]["Expression"]["SourceRef"]["Entity"], f[kind]["Property"]


def lit(v) -> dict:
    """A formatting literal: bool, number (D suffix) or text (quoted)."""
    if isinstance(v, bool):
        value = "true" if v else "false"
    elif isinstance(v, (int, float)):
        value = f"{v}D"
    else:
        value = "'" + str(v).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": value}}}


def color(hex_: str) -> dict:
    return {"solid": {"color": lit(hex_)}}


def proj(f: dict, label: str | None = None) -> dict:
    _, ent, prop = field_ref(f)
    p = {"field": f, "queryRef": f"{ent}.{prop}", "nativeQueryRef": prop}
    if label:
        p["displayName"] = label
    return p


def _literal(v) -> dict:
    if isinstance(v, bool):
        return {"Literal": {"Value": "true" if v else "false"}}
    if isinstance(v, int):
        return {"Literal": {"Value": f"{v}L"}}
    return {"Literal": {"Value": "'" + str(v).replace("'", "''") + "'"}}


def cat_filter(f: dict, values: list, key: str) -> dict:
    """Basic (categorical) filter: field IN values."""
    kind, ent, prop = field_ref(f)
    return {
        "name": _id("filter", key, ent, prop),
        "field": f,
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": ent, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{kind: {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}}],
                "Values": [[_literal(v)] for v in values],
            }}}],
        },
    }


def time_item(item: str, key: str) -> dict:
    """Apply one calculation item of the Time Intelligence group to a visual."""
    return cat_filter(column(*TI), [item], key)


CLOSED = ("closed", lambda key: cat_filter(column(PERIOD, "Is Closed"), [True], key))


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:20]


def rule_color(f: dict, rules: list[tuple[int, float, str]], default: str | None = None) -> dict:
    """Rule-based colour: first matching (comparison kind, threshold, colour). Kinds: 1 >, 2 >=, 3 <, 4 <=."""
    cases = [{"Condition": {"Comparison": {"ComparisonKind": k, "Left": f, "Right": {"Literal": {"Value": f"{t}D"}}}},
              "Value": {"Literal": {"Value": f"'{c}'"}}} for k, t, c in rules]
    cond: dict = {"Conditional": {"Cases": cases}}
    if default:
        cond["Conditional"]["DefaultValue"] = {"Literal": {"Value": f"'{default}'"}}
    return {"solid": {"color": {"expr": cond}}}


# ---------------------------------------------------------------------- layout
def box(col: int, span: int, row: tuple[int, int], *, dy: int = 0, h: int | None = None) -> dict:
    y, rh = row
    return {"x": M + col * (COL_W + G), "y": y + dy, "width": span * (COL_W + G) - G, "height": h or rh}


# ---------------------------------------------------------------------- plain-English copy (V3_DESIGN 10.2)
@functools.lru_cache(maxsize=1)
def _explanations() -> dict[str, dict[str, str]]:
    with EXPLANATIONS.open(encoding="utf-8", newline="") as fh:
        return {r["metric_id"]: r for r in csv.DictReader(fh)}


@functools.lru_cache(maxsize=1)
def _measure_metrics() -> dict[str, tuple[str, str]]:
    """Measure name -> (metric_id in metric_explanations.csv, format key)."""
    plan, _ = model.load_plan()
    out = {m.name: (m.metric, m.format) for m in plan.measures.values()}
    out.update({n: (model.IBNR_EXPLAINED[n], fmt) for n, (_, fmt, _) in model.IBNR_MEASURES.items()})
    return out


DIRECTION_WORDS = {"up": "Higher is better.", "down": "Lower is better.", "band": "Best inside its normal range."}


def _threshold(v: str, fmt: str) -> str:
    x = float(v)
    if fmt == "percent":
        return f"{x * 100:g}%"
    return f"${x:,.0f}" if fmt == "currency" else f"{x:,g}"


def meaning(measure_name: str) -> str:
    """A card's subtitle: what the number means, which way is good, and the watch / act levels in words."""
    metric, fmt = _measure_metrics()[measure_name]
    row = _explanations().get(metric)
    if row is None:
        raise KeyError(f"measure {measure_name!r} ({metric}): no row in {EXPLANATIONS.name}")
    words = [row["what_it_means"].strip(), DIRECTION_WORDS.get(row.get("good_direction", ""), "")]
    watch, alert = (row.get("watch_threshold") or "").strip(), (row.get("alert_threshold") or "").strip()
    if watch and alert:
        words.append(f"Watch at {_threshold(watch, fmt)}, act at {_threshold(alert, fmt)}.")
    elif alert:
        words.append(f"Act above {_threshold(alert, fmt)}." if float(alert) == 0
                     else f"Act at {_threshold(alert, fmt)}.")
    return " ".join(w for w in words if w)


# Each page: (what this page shows, why your boss asks). Plain words, no acronyms, at most 160 characters each.
PAGE_COPY: dict[str, tuple[str, str]] = {
    "executive": ("The whole book on one page: money taken in, money earned, what went to claims and how many "
                  "contracts are active.",
                  "Leaders use it to decide whether the business is growing at a profit and where to look next."),
    "dealer": ("Each dealer's sales, cancels, claims and payment habits over the last 12 closed months. "
               "Shaded rows need a look.",
               "It decides which dealers get a visit, a pricing review or a hold on new business."),
    "claims": ("How often claims come in, what they cost, how many are still open, and money set aside for "
               "claims nobody has reported yet.",
               "It shows whether claim costs are rising and whether enough is set aside to pay what is coming."),
    "regulatory": ("Each state's required claims reserve next to the reserve actually held, plus the lines filed "
                   "with the state.",
                   "A shortfall must be fixed before the state filing is due, so compliance checks this page first."),
    "finance": ("Profit and loss by product, the money shared with dealer-owned companies, and earned premium "
                "month, quarter and year to date.",
                "Finance uses it to close the month and to decide whether a dealer company can take a "
                "profit-share payout."),
    "experience": ("Everything to date on one basis. Pick View by to split the same total by date, product, "
                   "dealer, state, plan, make, year or term.",
                   "It answers 'which slice is driving the loss ratio?' without anyone building a new report."),
    "new_reserves": ("Claims reserve set aside on new sales, split by product and by contract length.",
                     "Finance and reinsurers read it to see how much new reserve each kind of contract brings in."),
}


class Page:
    def __init__(self, key: str, title: str, subtitle: str):
        self.name = _id("page", key)
        self.key, self.title, self.subtitle = key, title, subtitle
        self.visuals: list[dict] = []
        self.filters: list[dict] = []
        self.header()

    # ---- containers
    def add(self, key: str, pos: dict, visual: dict, *, filters: list[dict] | None = None,
            hidden: bool = False) -> dict:
        n = len(self.visuals)
        vc = {"$schema": S_VISUAL, "name": _id("visual", self.key, key),
              "position": {**pos, "z": n * 1000, "tabOrder": n * 1000}, "visual": visual}
        if filters:
            vc["filterConfig"] = {"filters": filters}
        if hidden:
            vc["isHidden"] = True
        self.visuals.append(vc)
        return vc

    @staticmethod
    def chrome(title: str | None, tip: str | None, *, border: bool = True, pad: int = 8,
               subtitle: str | None = None) -> dict:
        o: dict = {
            "background": [{"properties": {"show": lit(True), "color": color(SURFACE), "transparency": lit(0)}}],
            "border": [{"properties": {"show": lit(border), "color": color(BORDER), "radius": lit(0)}}],
            "dropShadow": [{"properties": {"show": lit(False)}}],
            "padding": [{"properties": {"top": lit(pad), "bottom": lit(pad), "left": lit(12), "right": lit(12)}}],
        }
        if title:
            o["title"] = [{"properties": {"show": lit(True), "text": lit(title), "fontColor": color(INK),
                                          "fontSize": lit(11), "bold": lit(True)}}]
        else:
            o["title"] = [{"properties": {"show": lit(False)}}]
        if subtitle:  # the plain-English line, visible without hovering
            o["subTitle"] = [{"properties": {"show": lit(True), "text": lit(subtitle), "titleWrap": lit(True),
                                             "fontColor": color(INK_2), "fontSize": lit(8)}}]
        if tip:
            o["visualHeader"] = [{"properties": {"show": lit(True), "showTooltipButton": lit(True)}}]
            o["visualHeaderTooltip"] = [{"properties": {"text": lit(tip)}}]
        return o

    def text(self, key: str, pos: dict, paragraphs: list[tuple[str, str, str, bool]], *, card: bool = False):
        """Text box; paragraphs = (text, size, colour, bold)."""
        paras = [{"textRuns": [{"value": t, "textStyle": {"fontSize": s, "color": c,
                                                          **({"fontWeight": "bold"} if b else {})}}]}
                 for t, s, c, b in paragraphs]
        v = {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paras}}]},
             "visualContainerObjects": {
                 "background": [{"properties": {"show": lit(card), "color": color(SURFACE)}}],
                 "border": [{"properties": {"show": lit(card), "color": color(BORDER), "radius": lit(0)}}],
                 "title": [{"properties": {"show": lit(False)}}]}}
        return self.add(key, pos, v)

    def header(self) -> None:
        """Wordmark image, page title, then the plain-English card (what it shows, why your boss asks, details)."""
        logo = {"visualType": "image",
                "objects": {"general": [{"properties": {"imageUrl": {"expr": {"ResourcePackageItem": {
                    "PackageName": "RegisteredResources", "PackageType": 1, "ItemName": WORDMARK}}}}}],
                    "imageScaling": [{"properties": {"imageScalingType": lit("Fit")}}]},
                "visualContainerObjects": {
                    "background": [{"properties": {"show": lit(False)}}],
                    "border": [{"properties": {"show": lit(False)}}],
                    "title": [{"properties": {"show": lit(False)}}]}}
        self.add("logo", box(0, 2, ROW_HEADER), logo)
        self.text("header", box(2, 10, ROW_HEADER),
                  [(self.title, "18pt", INK, True), (f"{model.MODEL_NAME} · {model.SUBTITLE}", "9pt", INK_3, False)])
        shows, boss = PAGE_COPY[self.key]
        self.text("explain", box(0, 12, ROW_EXPLAIN), [
            ("What this page shows: " + shows, "10pt", INK, False),
            ("Why your boss asks: " + boss, "10pt", INK, False),
            ("Details: " + self.subtitle, "8pt", INK_3, False),
        ], card=True)

    # ---- visuals
    def card(self, key: str, pos: dict, f: dict, title: str, tip: str, *, item: str | None = "T12M",
             filters: list[dict] | None = None):
        v = {"visualType": "card",
             "query": {"queryState": {"Values": {"projections": [proj(f, title)]}}},
             "objects": {"labels": [{"properties": {"color": color(INK), "fontSize": lit(20)}}],
                         "categoryLabels": [{"properties": {"show": lit(False)}}]},
             "visualContainerObjects": self.chrome(f"{title} · {item}" if item else title, tip,
                                                   subtitle=meaning(field_ref(f)[2])),
             "drillFilterOtherVisuals": True}
        flt = ([time_item(item, key)] if item else []) + (filters or [])
        return self.add(key, pos, v, filters=flt or None)

    def slicer(self, key: str, pos: dict, f: dict, label: str, *, single: bool = False,
               sync: str | None = None, hidden: bool = False):
        objects: dict = {"data": [{"properties": {"mode": lit("Dropdown")}}],
                         "header": [{"properties": {"show": lit(True), "text": lit(label), "fontColor": color(INK_2)}}],
                         "items": [{"properties": {"fontColor": color(INK)}}]}
        if single:
            objects["selection"] = [{"properties": {"singleSelect": lit(True)}}]
        v = {"visualType": "slicer",
             "query": {"queryState": {"Values": {"projections": [proj(f)]}}},
             "objects": objects,
             "visualContainerObjects": self.chrome(None, None, pad=2),
             "drillFilterOtherVisuals": True}
        if sync:
            v["syncGroup"] = {"groupName": sync, "fieldChanges": False, "filterChanges": True}
        return self.add(key, pos, v, hidden=hidden)

    def chart(self, key: str, pos: dict, vtype: str, roles: dict[str, list[dict]], title: str, tip: str, *,
              filters: list[dict] | None = None, sort: tuple[dict, str] | None = None,
              colors: list[str] | None = None, objects: dict | None = None,
              params: dict[str, dict] | None = None):
        """``params``: role -> field-parameter column, projected at index 0 of that role."""
        qs = {role: {"projections": ps} for role, ps in roles.items() if ps}
        for role, f in (params or {}).items():
            qs[role]["fieldParameters"] = [{"parameterExpr": f, "index": 0, "length": 1}]
        query: dict = {"queryState": qs}
        if sort:
            query["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}], "isDefaultSort": False}
        obj = dict(objects or {})
        if colors:
            sels = []
            for i, p in enumerate([p for r in ("Y", "Y2", "Values") for p in (roles.get(r) or [])][:len(colors)]):
                sels.append({"properties": {"fill": color(colors[i])}, "selector": {"metadata": p["queryRef"]}})
            obj["dataPoint"] = sels
        if vtype not in ("tableEx", "pivotTable"):
            obj.setdefault("legend", [{"properties": {"show": lit(True), "position": lit("Top")}}])
        v = {"visualType": vtype, "query": query, "visualContainerObjects": self.chrome(title, tip, subtitle=tip),
             "drillFilterOtherVisuals": True}
        if obj:
            v["objects"] = obj
        return self.add(key, pos, v, filters=filters)

    # ---- files
    def page_json(self) -> dict:
        p = {"$schema": S_PAGE, "name": self.name, "displayName": self.title, "displayOption": "FitToPage",
             "height": H, "width": W,
             "objects": {"background": [{"properties": {"color": color(PAGE_BG), "transparency": lit(0)}}],
                         "outspace": [{"properties": {"color": color(PAGE_BG)}}]}}
        if self.filters:
            p["filterConfig"] = {"filters": self.filters}
        return p


# ---------------------------------------------------------------------- pages
NOTE_T12 = "Trailing 12 closed months (Time Intelligence = T12M) unless the title says otherwise."


def executive() -> Page:
    p = Page("executive", "Executive overview",
             "Written and earned premium, loss ratio and the in-force book. " + NOTE_T12 +
             " Source: vsc_analytics on the local PostgreSQL server.")
    p.slicer("tenant", box(0, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("category", box(3, 3, ROW_FILTERS), column("Product", "Category"), "Product category")
    p.slicer("year", box(6, 3, ROW_FILTERS), column(PERIOD, "Fiscal Year"), "Fiscal year")
    kpis = [
        ("Written Premium", "Written premium", "Coverage cost written in the window (Contract Month fact)."),
        ("Earned Premium", "Earned premium", "Premium earned in the window on the earning curves."),
        ("Loss Ratio (on earned premium)", "Loss ratio", "Accident-month incurred claims over earned premium."),
        ("Combined Ratio", "Combined ratio", "Claims, admin fee and acquisition cost over earned disbursements."),
        ("Contracts In Force", "Contracts in force", "Balance at the last closed period (never a partial month)."),
        ("Claim Frequency", "Claim frequency", "Claims per exposure year."),
    ]
    for i, (m, label, tip) in enumerate(kpis):
        item = None if m == "Contracts In Force" else "T12M"
        p.card(f"kpi{i}", box(2 * i, 2, ROW_KPI), measure(m), label, tip, item=item)
    period = column(PERIOD, "Period Start")
    p.chart("trend", box(0, 8, ROW_A), "lineChart",
            {"Category": [proj(period, "Period")],
             "Y": [proj(measure("Written Premium")), proj(measure("Earned Premium"))],
             "Tooltips": [proj(measure("Loss Ratio (on earned premium)"))]},
            "Written vs earned premium by posting period",
            "Monthly written and earned premium, closed periods only. Earned lags written: the gap is the "
            "unearned premium reserve building up.",
            filters=[CLOSED[1]("trend")], colors=[SERIES[0], SERIES[1]])
    p.chart("lr_year", box(8, 4, ROW_A), "clusteredColumnChart",
            {"Category": [proj(column(PERIOD, "Fiscal Year"), "Fiscal year")],
             "Y": [proj(measure("Loss Ratio (on earned premium)"), "Loss ratio")]},
            "Loss ratio by fiscal year",
            "Accident-month loss ratio on earned premium, by fiscal year (closed periods).",
            filters=[CLOSED[1]("lr_year")], colors=[ACCENT],
            objects={"labels": [{"properties": {"show": lit(True), "color": color(INK_2)}}]})
    p.chart("by_product", box(0, 6, ROW_B), "clusteredBarChart",
            {"Category": [proj(column("Product", "Category"), "Product category")],
             "Y": [proj(measure("Written Premium")), proj(measure("Earned Premium"))]},
            "Written and earned premium by product category · T12M",
            "Trailing-12 written and earned premium by product category.",
            filters=[time_item("T12M", "by_product")], sort=(measure("Written Premium"), "Descending"),
            colors=[SERIES[0], SERIES[1]])
    p.chart("lr_trend", box(6, 6, ROW_B), "lineChart",
            {"Category": [proj(period, "Period")],
             "Y": [proj(measure("Loss Ratio (on earned premium)"), "Loss ratio, T12M")]},
            "Loss ratio, trailing 12 months, by period",
            "Each point is the trailing-12 loss ratio ending in that period (T12M calculation item).",
            filters=[CLOSED[1]("lr_trend"), time_item("T12M", "lr_trend")], colors=[SERIES[7]])
    return p


SC = "Scorecard Loss Ratio (on claims reserve)"


def dealer() -> Page:
    p = Page("dealer", "Dealer scorecard",
             "Dealer x month scorecard rolled to the trailing 12 closed months. Red: loss ratio of 70% or more, "
             "or late remittance rate of 20% or more. Row-level security: a Dealer User sees only their dealers.")
    p.slicer("tenant", box(0, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("group", box(3, 3, ROW_FILTERS), column("Dealer Group", "Group Name"), "Dealer group")
    p.slicer("state", box(6, 3, ROW_FILTERS), column("Dealer", "State"), "Dealer state")
    p.slicer("status", box(9, 3, ROW_FILTERS), column("Dealer", "Dealer Status"), "Dealer status")
    kpis = [
        ("Dealers On Watch", "Dealers on watch", "Dealers flagged on any watch rule at the last closed month."),
        ("Dealers on Loss-Ratio Watch", "Loss-ratio watch", "Dealers above the loss-ratio watch threshold."),
        (SC, "Loss ratio", "Incurred claims over earned claims reserve, trailing 12."),
        ("Scorecard Cancel Rate", "Cancel rate", "Contracts cancelled over contracts written, trailing 12."),
        ("Early Claim Rate", "Early-claim rate", "Claims inside the early-claim window over claims reported."),
        ("Scorecard Late Remit Rate", "Late remit rate", "Late remittances over remittances, trailing 12."),
    ]
    for i, (m, label, tip) in enumerate(kpis):
        item = None if "Watch" in m else "T12M"
        p.card(f"kpi{i}", box(2 * i, 2, ROW_KPI), measure(m), label, tip, item=item)
    vals = [
        proj(column("Dealer", "Dealer Name"), "Dealer"),
        proj(column("Dealer", "State"), "State"),
        proj(measure("Scorecard Contracts Written"), "Written"),
        proj(measure("Scorecard Cancel Rate"), "Cancel rate"),
        proj(measure("Scorecard Earned Claims Reserve"), "Earned claims reserve"),
        proj(measure("Scorecard Incurred Claims"), "Incurred"),
        proj(measure(SC), "Loss ratio"),
        proj(measure("Early Claim Rate"), "Early-claim rate"),
        proj(measure("Scorecard Late Remit Rate"), "Late remit rate"),
        proj(measure("Scorecard Average Days Late"), "Avg days late"),
    ]
    lr = measure(SC)
    late = measure("Scorecard Late Remit Rate")
    cf = [
        {"properties": {"backColor": rule_color(lr, [(2, 0.7, CRITICAL_SOFT), (2, 0.55, WARNING_SOFT)])},
         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"{MEASURES}.{SC}"}},
        {"properties": {"backColor": rule_color(late, [(2, 0.2, CRITICAL_SOFT), (2, 0.1, WARNING_SOFT)])},
         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                      "metadata": f"{MEASURES}.Scorecard Late Remit Rate"}},
    ]
    p.chart("table", {**box(0, 9, ROW_A), "height": ROW_B[0] + ROW_B[1] - ROW_A[0]}, "tableEx", {"Values": vals},
            "Dealer scorecard · T12M",
            "One row per dealer, trailing 12 closed months. Ratios are rebuilt from monthly additive columns, "
            "so a group or tenant total is a true ratio, never an average of dealer ratios.",
            filters=[time_item("T12M", "table")], sort=(lr, "Descending"),
            objects={"values": cf,
                     "columnHeaders": [{"properties": {"fontColor": color(INK_2), "bold": lit(True)}}],
                     "grid": [{"properties": {"gridHorizontalColor": color(GRID)}}]})
    p.chart("watch", box(9, 3, ROW_A), "clusteredBarChart",
            {"Category": [proj(column("Dealer", "State"), "State")], "Y": [proj(lr, "Loss ratio")]},
            "Loss ratio by dealer state · T12M", "Scorecard loss ratio by dealer state, trailing 12.",
            filters=[time_item("T12M", "watch")], sort=(lr, "Descending"), colors=[SERIES[7]])
    p.text("rls", box(9, 3, ROW_B), [
        ("Row-level security", "11pt", INK, True),
        ("Dealer User: only the dealers mapped to the signed-in login. Tenant Partner: every dealer of the "
         "user's programs. Internal: everything.", "9pt", INK_2, False),
        ("Test it: Modeling > View as > tick Other user, type a user_login from core.dim_user_dealer_access, "
         "tick Dealer User. The filing and IBNR pages go blank for external roles (internal only).",
         "9pt", INK_2, False),
    ], card=True)
    return p


def claims() -> Page:
    p = Page("claims", "Claims & IBNR",
             "Frequency, severity, denial and open claims by loss month, plus actuarial IBNR by accident quarter "
             "(valuation output: the slicers do not filter it). T12M unless the title says otherwise.")
    p.slicer("tenant", box(0, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("category", box(3, 3, ROW_FILTERS), column("Product", "Category"), "Product category")
    p.slicer("status", box(6, 3, ROW_FILTERS), column("Claim Summary", "Status Group"), "Claim status group")
    kpis = [
        ("Claim Frequency", "Claim frequency", "T12M", "Claims per exposure year."),
        ("Paid Claim Severity", "Paid severity", "T12M", "Paid amount per claim closed with payment."),
        ("Denial Rate", "Denial rate", "T12M", "Claims denied over claims reported."),
        ("Open Claims", "Open claims", None, "Claims open at the as-of date."),
        ("Open Claims Aged 30 Plus", "Open 30+ days", None, "Open claims older than 30 days."),
        ("IBNR (selected)", "IBNR (selected)", None, "Selected IBNR at the valuation date, all cohorts."),
    ]
    for i, (m, label, item, tip) in enumerate(kpis):
        ent = "IBNR by Cohort" if m.startswith("IBNR") else MEASURES
        p.card(f"kpi{i}", box(2 * i, 2, ROW_KPI), measure(m, ent), label, tip, item=item)
    p.chart("freq_sev", box(0, 8, ROW_A), "lineClusteredColumnComboChart",
            {"Category": [proj(column(PERIOD, "Period Start"), "Loss month")],
             "Y": [proj(measure("Claims Reported (loss month)"), "Claims reported")],
             "Y2": [proj(measure("Paid Claim Severity"), "Paid severity")]},
            "Claims reported (columns) and paid severity (line) by loss month",
            "Claim counts by loss month from Claim Summary (both admin systems), with the paid severity of the "
            "same claims. Recent months are still developing: see IBNR.",
            filters=[CLOSED[1]("freq_sev")], colors=[SERIES[0], SERIES[1]])
    p.chart("aging", box(8, 4, ROW_A), "clusteredBarChart",
            {"Category": [proj(column("Claim Summary", "Open Age Band"), "Age band")],
             "Y": [proj(measure("Open Claims"))]},
            "Open claims by age band", "Open-claim inventory by days open at the as-of date.",
            sort=(column("Claim Summary", "Open Age Band"), "Ascending"), colors=[SERIES[3]])
    p.chart("components", box(0, 5, ROW_B), "clusteredBarChart",
            {"Category": [proj(column("Claim Summary", "Top Component"), "Component")],
             "Y": [proj(measure("Paid On Paid Claims"), "Paid")],
             "Tooltips": [proj(measure("Paid Claim Count")), proj(measure("Paid Claim Severity"))]},
            "Top components by paid claims · T12M",
            "Paid amount on claims closed with payment, by the claim's top component (largest line). Scroll for more.",
            filters=[time_item("T12M", "components")], sort=(measure("Paid On Paid Claims"), "Descending"),
            colors=[SERIES[2]])
    ibnr = "IBNR by Cohort"
    p.chart("ibnr", box(5, 7, ROW_B), "lineClusteredColumnComboChart",
            {"Category": [proj(column(ibnr, "Cohort Label"), "Accident quarter")],
             "Y": [proj(measure("Paid To Date (actuarial)", ibnr), "Paid to date"),
                   proj(measure("IBNR (selected)", ibnr), "IBNR")],
             "Y2": [proj(measure("Ultimate Loss Ratio (actuarial)", ibnr), "Ultimate loss ratio")]},
            "Paid to date, IBNR and ultimate loss ratio by accident quarter",
            "Actuarial valuation (chain ladder / Bornhuetter-Ferguson). Young quarters carry most of the IBNR.",
            sort=(column(ibnr, "Cohort Label"), "Ascending"), colors=[SERIES[0], SERIES[3], SERIES[7]],
            objects={"legend": [{"properties": {"show": lit(True), "position": lit("Top")}}]})
    return p


def regulatory() -> Page:
    p = Page("regulatory", "Regulatory funded reserve",
             "Funded-reserve filing by state and provider. Scope defaults to ITD (each state's latest filing); "
             "pick PERIOD to see the filing window. Lines come from the filed line values, sections 2 and 3.")
    frf, rlv = "Funded Reserve Filing", "Regulatory Line Value"
    # One visible slicer per field; a hidden twin in the same sync group filters the other filing fact.
    p.slicer("state", box(0, 3, ROW_FILTERS), column(rlv, "State"), "State", sync="state")
    p.slicer("state_twin", box(0, 3, ROW_FILTERS), column(frf, "State"), "State", sync="state", hidden=True)
    p.slicer("scope", box(3, 2, ROW_FILTERS), column(rlv, "Scope"), "Scope", single=True, sync="scope")
    p.slicer("scope_twin", box(3, 2, ROW_FILTERS), column(frf, "Scope"), "Scope", single=True, sync="scope",
             hidden=True)
    p.slicer("provider", box(5, 4, ROW_FILTERS), column("Obligor", "Obligor Name"), "Provider (obligor)")
    kpis = [
        ("Calculated Reserve", "Calculated reserve", "Required reserve by the state's method (rate x basis)."),
        ("Current Reserve Held", "Reserve held", "Reserve held at the filing date."),
        ("Reserve Shortfall", "Shortfall", "Calculated minus held, where positive."),
        ("Complaints Received", "Complaints", "Complaints received in the filing window."),
    ]
    for i, (m, label, tip) in enumerate(kpis):
        p.card(f"kpi{i}", box(3 * i, 3, ROW_KPI), measure(m), label, tip, item=None)
    short = measure("Reserve Shortfall")
    p.chart("by_state", {**box(0, 6, ROW_A), "height": ROW_B[0] + ROW_B[1] - ROW_A[0]}, "tableEx",
            {"Values": [proj(column(frf, "State"), "State"), proj(column(frf, "Provider Name"), "Provider"),
                        proj(column(frf, "Method Code"), "Method"),
                        proj(measure("Calculated Reserve"), "Calculated"),
                        proj(measure("Current Reserve Held"), "Held"), proj(short, "Shortfall")]},
            "Funded reserve by state and provider",
            "Each state's filing at the default basis. Red rows hold less than the calculated reserve.",
            sort=(short, "Descending"),
            objects={"values": [{"properties": {"backColor": rule_color(short, [(1, 0, CRITICAL_SOFT)])},
                                 "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                              "metadata": f"{MEASURES}.Reserve Shortfall"}}],
                     "columnHeaders": [{"properties": {"fontColor": color(INK_2), "bold": lit(True)}}]})
    p.chart("lines", {**box(6, 6, ROW_A), "height": ROW_B[0] + ROW_B[1] - ROW_A[0]}, "tableEx",
            {"Values": [proj(column(rlv, "Section"), "Section"), proj(column(rlv, "Line No"), "Line"),
                        proj(column(rlv, "Label"), "Line item"), proj(column(rlv, "Line Item"), "Code"),
                        proj(column(rlv, "Basis"), "Basis"), proj(measure("Filing Line Value"), "Value")]},
            "Filed lines, sections 2 and 3",
            "Filing Line Value needs one report, line, scope and basis in context, so each row is one filed line "
            "(a LAST line such as in force shows its latest window).",
            filters=[cat_filter(column(rlv, "Report ID"), ["FUNDED_RESERVE"], "lines"),
                     cat_filter(column(rlv, "Section"), [2, 3], "lines")],
            sort=(column(rlv, "Line No"), "Ascending"))
    return p


def finance() -> Page:
    p = Page("finance", "Finance",
             "P&L by product on the posted ledger and the dealer cession (claims reserve and profit share). "
             + NOTE_T12)
    p.slicer("tenant", box(0, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("structure", box(3, 3, ROW_FILTERS), column("Dealer", "Participation Structure"),
             "Participation structure")
    p.slicer("year", box(6, 3, ROW_FILTERS), column(PERIOD, "Fiscal Year"), "Fiscal year")
    kpis = [
        ("Earned Disbursement Amount", "Earned premium", "Earned disbursements (all buckets) in the window."),
        ("Earned Reserve", "Earned reserve", "Claims-reserve bucket earned in the window."),
        ("Earned Admin Fee", "Earned admin fee", "Administrator fee earned in the window."),
        ("Loss Ratio (on claims reserve)", "Loss ratio", "Claims incurred over earned claims reserve."),
        ("Disbursed Profit Share Reserve", "Profit share", "Profit-share reserve ceded to dealers."),
        ("Commissions", "Commissions", "Agent and dealer commissions posted."),
    ]
    for i, (m, label, tip) in enumerate(kpis):
        p.card(f"kpi{i}", box(2 * i, 2, ROW_KPI), measure(m), label, tip)
    pl = [proj(measure("Gross Coverage Cost"), "Written"), proj(measure("Earned Disbursement Amount"), "Earned"),
          proj(measure("Earned Reserve"), "Earned reserve"), proj(measure("Claims Incurred"), "Claims incurred"),
          proj(measure("Earned Admin Fee"), "Admin fee"), proj(measure("Earned Acquisition Cost"), "Acquisition"),
          proj(measure("Combined Ratio"), "Combined ratio")]
    cr = measure("Combined Ratio")
    p.chart("pl", box(0, 8, ROW_A), "pivotTable",
            {"Rows": [proj(column("Product", "Category"), "Category"), proj(column("Product", "Product"), "Product")],
             "Values": pl},
            "P&L by product · T12M",
            "Expand a category to see its products. Combined ratio = claims + admin fee + acquisition over earned.",
            filters=[time_item("T12M", "pl")],
            objects={"values": [{"properties": {"fontColor": rule_color(cr, [(2, 1.0, CRITICAL)])},
                                 "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                              "metadata": f"{MEASURES}.Combined Ratio"}}],
                     "columnHeaders": [{"properties": {"fontColor": color(INK_2), "bold": lit(True)}}]})
    p.chart("ti", box(8, 4, ROW_A), "pivotTable",
            {"Rows": [proj(column("Product", "Category"), "Category")],
             "Columns": [proj(column(*TI), "Window")],
             "Values": [proj(measure("Earned Disbursement Amount"), "Earned")]},
            "Earned premium: MTD, QTD, YTD",
            "One measure, three windows: the Time Intelligence calculation group on the columns. No MTD/YTD "
            "copies of any measure exist in the model.",
            filters=[cat_filter(column(*TI), ["MTD", "QTD", "YTD"], "ti")])
    p.chart("cession", box(0, 6, ROW_B), "clusteredColumnChart",
            {"Category": [proj(column("Dealer", "Participation Structure"), "Structure")],
             "Y": [proj(measure("Disbursed Claims Reserve"), "Claims reserve ceded"),
                   proj(measure("Disbursed Profit Share Reserve"), "Profit-share reserve"),
                   proj(measure("Claims Paid"), "Claims paid")]},
            "Cession by participation structure · T12M",
            "Claims reserve and profit-share reserve disbursed to dealer participation structures, against "
            "claims paid on the same business.",
            filters=[time_item("T12M", "cession")], colors=[SERIES[0], SERIES[6], SERIES[1]])
    p.chart("earned_trend", box(6, 6, ROW_B), "lineChart",
            {"Category": [proj(column(PERIOD, "Period Start"), "Period")],
             "Y": [proj(measure("Earned Disbursement Amount"), "Earned"),
                   proj(measure("Claims Incurred"), "Incurred")]},
            "Earned premium vs claims incurred by posting period",
            "Posted ledger view by month, closed periods only.",
            filters=[CLOSED[1]("earned_trend")], colors=[SERIES[0], SERIES[7]])
    return p


VIEW = model.EXPERIENCE_VIEW
LR_RESERVE = "Loss Ratio (on claims reserve)"


def experience() -> Page:
    p = Page("experience", "Experience",
             "Everything to date on the claims-reserve basis, closed periods only. View by switches the rows and "
             "bars: Date, Product, Dealer, State, Plan, Make, Entered year, Term. Every view adds up to the same "
             "total. Site twin: finance/experience.html (finance.mart_experience).")
    p.filters.append(CLOSED[1]("experience_page"))
    view = column(VIEW, VIEW)
    p.slicer("view", box(0, 3, ROW_FILTERS), view, "View by", single=True)
    p.slicer("tenant", box(3, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("category", box(6, 3, ROW_FILTERS), column("Product", "Category"), "Product category")
    p.slicer("structure", box(9, 3, ROW_FILTERS), column("Dealer", "Participation Structure"),
             "Participation structure")
    kpis = [
        ("Disbursed Claims Reserve", "Net written reserve", "Claims reserve written net of cancellations, to date."),
        ("Earned Reserve", "Earned reserve", "Claims reserve earned to date on the earning curves."),
        ("Earned Pct", "Earned %", "Earned reserve over net written reserve."),
        ("Cancel Pct", "Cancel %", "Effective cancellations (not reinstated) over contracts written."),
        ("Claims Paid", "Claims paid", "Claim dollars paid to date."),
        (LR_RESERVE, "Loss ratio", "Claims incurred over earned claims reserve."),
    ]
    for i, (m, label, tip) in enumerate(kpis):
        p.card(f"kpi{i}", box(2 * i, 2, ROW_KPI), measure(m), label, tip, item=None)
    vals = [proj(view, "View by"), proj(measure("Contracts Written"), "Contracts"),
            proj(measure("Disbursed Claims Reserve"), "Net written"), proj(measure("Earned Reserve"), "Earned"),
            proj(measure("Earned Pct"), "Earned %"), proj(measure("Effective Cancellations"), "Cancelled"),
            proj(measure("Cancel Pct"), "Cancel %"), proj(measure("Claims Paid"), "Claims paid"),
            proj(measure(LR_RESERVE), "Loss ratio")]
    lr = measure(LR_RESERVE)
    p.chart("table", {**box(0, 8, ROW_A), "height": ROW_B[0] + ROW_B[1] - ROW_A[0]}, "tableEx", {"Values": vals},
            "Experience by the View by pick",
            "One row per value of the View by pick. Ratios are rebuilt from additive measures, so the total row "
            "is a true ratio and is the same in every view.",
            params={"Values": view}, sort=(measure("Disbursed Claims Reserve"), "Descending"),
            objects={"values": [{"properties": {"backColor": rule_color(lr, [(2, 0.7, CRITICAL_SOFT),
                                                                               (2, 0.55, WARNING_SOFT)])},
                                 "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                              "metadata": f"{MEASURES}.{LR_RESERVE}"}}],
                     "columnHeaders": [{"properties": {"fontColor": color(INK_2), "bold": lit(True)}}],
                     "grid": [{"properties": {"gridHorizontalColor": color(GRID)}}]})
    p.chart("lr", box(8, 4, ROW_A), "clusteredBarChart",
            {"Category": [proj(view, "View by")], "Y": [proj(lr, "Loss ratio")]},
            "Loss ratio by the View by pick", "Claims incurred over earned claims reserve, to date.",
            params={"Category": view}, sort=(lr, "Descending"), colors=[SERIES[7]])
    p.chart("pcts", box(8, 4, ROW_B), "clusteredBarChart",
            {"Category": [proj(view, "View by")],
             "Y": [proj(measure("Earned Pct"), "Earned %"), proj(measure("Cancel Pct"), "Cancel %")]},
            "Earned % and Cancel % by the View by pick",
            "How far each slice has earned, and how much of it cancelled.",
            params={"Category": view}, sort=(measure("Earned Pct"), "Descending"), colors=[SERIES[0], SERIES[1]])
    return p


NEW_RESERVE = "Disbursed Claims Reserve"


def _sale(key: str) -> list[dict]:
    """New reserve = the claims reserve booked at sale: Disbursement rows with Txn Type SALE only."""
    return [cat_filter(column("Disbursement", "Txn Type"), ["SALE"], key)]


def new_reserves() -> Page:
    p = Page("new_reserves", "New Reserves",
             "Gross claims reserve booked at sale (before any cancel: the new-business basis a reinsurer reads), "
             "by product category and term. " + NOTE_T12 + " Site twin: finance/new-reserves.html "
             "(finance.mart_new_reserves_by_term) with the average and row-% matrices.")
    p.slicer("tenant", box(0, 3, ROW_FILTERS), column("Tenant", "Reportable Name"), "Program (tenant)")
    p.slicer("category", box(3, 3, ROW_FILTERS), column("Product", "Category"), "Product category")
    p.slicer("year", box(6, 3, ROW_FILTERS), column(PERIOD, "Fiscal Year"), "Fiscal year")
    term = column("Contract Terms", "Term Months")
    p.slicer("term", box(9, 3, ROW_FILTERS), term, "Term (months)")
    nr = measure(NEW_RESERVE)
    p.card("kpi0", box(0, 3, ROW_KPI), nr, "New reserve", "Claims reserve on sale postings (Txn Type SALE).",
           filters=_sale("kpi0"))
    p.card("kpi1", box(3, 3, ROW_KPI), measure("Contracts Written"), "Contracts written",
           "Contracts sold in the window (sales only).")
    p.card("kpi2", box(6, 3, ROW_KPI), nr, "Net written reserve",
           "Claims reserve on every posting (sales, cancels, amendments): what is left after cancels.")
    p.card("kpi3", box(9, 3, ROW_KPI), measure("Effective Cancellations"), "Effective cancellations",
           "Cancellations not later reinstated.")
    p.chart("by_category", box(0, 6, ROW_A), "clusteredBarChart",
            {"Category": [proj(column("Product", "Category"), "Product category")], "Y": [proj(nr, "New reserve")],
             "Tooltips": [proj(measure("Contracts Written"), "Contracts")]},
            "New reserve by product category · T12M", "Claims reserve booked at sale, trailing 12 closed months.",
            filters=[time_item("T12M", "by_category"), *_sale("by_category")], sort=(nr, "Descending"),
            colors=[SERIES[0]])
    p.chart("by_term", box(6, 6, ROW_A), "clusteredColumnChart",
            {"Category": [proj(term, "Term (months)")], "Y": [proj(nr, "New reserve")]},
            "New reserve by term (months) · T12M", "Longer terms carry more reserve per contract.",
            filters=[time_item("T12M", "by_term"), *_sale("by_term")], sort=(term, "Ascending"), colors=[SERIES[6]])
    p.chart("matrix", box(0, 12, ROW_B), "pivotTable",
            {"Rows": [proj(column("Product", "Category"), "Category")], "Columns": [proj(term, "Term (months)")],
             "Values": [proj(nr, "New reserve"), proj(measure("Contracts Written"), "Contracts")]},
            "New reserve and contracts by category x term · T12M",
            "Contracts come from the sales fact, so the Txn Type filter does not change them.",
            filters=[time_item("T12M", "matrix"), *_sale("matrix")],
            objects={"columnHeaders": [{"properties": {"fontColor": color(INK_2), "bold": lit(True)}}]})
    return p


PAGES = (executive, dealer, claims, regulatory, finance, experience, new_reserves)


# ---------------------------------------------------------------------- theme + report files
AXIS = {"gridlineColor": {"solid": {"color": GRID}}, "labelColor": {"solid": {"color": INK_2}},
        "titleColor": {"solid": {"color": INK_2}}}


def _solid(c: str) -> dict:
    return {"solid": {"color": c}}


def theme() -> dict:
    """F&IP dark theme. Green is an accent and a border; any green fill gets black text (INK_ON_BRAND)."""
    return {
        "name": model.MODEL_NAME,
        "dataColors": SERIES,
        "foreground": INK, "foregroundNeutralSecondary": INK_2, "foregroundNeutralTertiary": INK_3,
        "foregroundDark": INK_ON_BRAND, "foregroundLight": INK, "foregroundSelected": INK_ON_BRAND,
        "foregroundButton": INK_ON_BRAND,
        "background": SURFACE, "backgroundLight": SURFACE_2, "backgroundDark": PAGE_BG, "backgroundNeutral": BORDER,
        "secondaryBackground": SURFACE_2,
        "tableAccent": BRAND, "accent": BRAND, "hyperlink": BRAND, "visitedHyperlink": BRAND,
        "disabledText": INK_3,
        "good": GOOD, "neutral": WARNING, "bad": CRITICAL,
        "maximum": CRITICAL, "center": WARNING, "minimum": SURFACE_2, "null": GRID,
        "textClasses": {
            "title": {"fontFace": FONT_HEAD, "fontSize": 12, "color": INK},
            "header": {"fontFace": FONT_HEAD, "fontSize": 11, "color": INK},
            "label": {"fontFace": FONT_BODY, "fontSize": 9, "color": INK_2},
            "callout": {"fontFace": FONT_NUM, "fontSize": 20, "color": INK},
        },
        "visualStyles": {
            "*": {"*": {
                "background": [{"show": True, "color": _solid(SURFACE), "transparency": 0}],
                "border": [{"show": True, "color": _solid(BORDER), "radius": 0}],
                "dropShadow": [{"show": False}],
                "title": [{"fontColor": _solid(INK), "fontSize": 12, "bold": True, "fontFamily": FONT_HEAD}],
                "subTitle": [{"fontColor": _solid(INK_2), "fontSize": 8}],
                "categoryAxis": [AXIS],
                "valueAxis": [AXIS],
                "legend": [{"labelColor": _solid(INK_2)}],
                "visualHeader": [{"foreground": _solid(INK_2), "background": _solid(SURFACE)}],
                "visualTooltip": [{"titleFontColor": _solid(INK), "valueFontColor": _solid(INK),
                                   "background": _solid(SURFACE_2)}],
            }},
            "page": {"*": {
                "background": [{"color": _solid(PAGE_BG), "transparency": 0}],
                "outspace": [{"color": _solid(PAGE_BG)}],
            }},
            "slicer": {"*": {
                "items": [{"fontColor": _solid(INK), "background": _solid(SURFACE_2)}],
                "header": [{"fontColor": _solid(INK_2)}],
            }},
            "tableEx": {"*": {
                "columnHeaders": [{"fontColor": _solid(INK), "backColor": _solid(SURFACE_2)}],
                "values": [{"fontColorPrimary": _solid(INK), "backColorPrimary": _solid(SURFACE),
                            "fontColorSecondary": _solid(INK), "backColorSecondary": _solid(SURFACE_2)}],
                "total": [{"fontColor": _solid(INK), "backColor": _solid(SURFACE_2)}],
                "grid": [{"gridHorizontalColor": _solid(GRID), "gridVerticalColor": _solid(GRID)}],
            }},
            "pivotTable": {"*": {
                "columnHeaders": [{"fontColor": _solid(INK), "backColor": _solid(SURFACE_2)}],
                "rowHeaders": [{"fontColor": _solid(INK), "backColor": _solid(SURFACE)}],
                "values": [{"fontColorPrimary": _solid(INK), "backColorPrimary": _solid(SURFACE),
                            "fontColorSecondary": _solid(INK), "backColorSecondary": _solid(SURFACE_2)}],
                "subTotals": [{"fontColor": _solid(INK), "backColor": _solid(SURFACE_2)}],
                "grid": [{"gridHorizontalColor": _solid(GRID), "gridVerticalColor": _solid(GRID)}],
            }},
        },
    }


THEME_FILE = "FIP_Theme.json"


def report_files(model_dir_name: str) -> dict[str, dict]:
    """Relative path (inside ``<name>.Report/``) -> JSON document."""
    pages = [f() for f in PAGES]
    files: dict[str, dict] = {
        "definition.pbir": {"$schema": S_PBIR, "version": "4.0",
                            "datasetReference": {"byPath": {"path": f"../{model_dir_name}"}}},
        "definition/version.json": {"$schema": S_VERSION, "version": "2.0.0"},
        "definition/report.json": {
            "$schema": S_REPORT,
            "themeCollection": {"customTheme": {
                "name": THEME_FILE, "type": "RegisteredResources",
                "reportVersionAtImport": {"visual": "2.13.0", "report": "3.3.0", "page": "2.1.0"}}},
            "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources",
                                  "items": [{"name": THEME_FILE, "path": THEME_FILE, "type": "CustomTheme"},
                                            {"name": WORDMARK, "path": WORDMARK, "type": "Image"}]}],
            "settings": {"useStylableVisualContainerHeader": True, "exportDataMode": "AllowSummarized",
                         "defaultDrillFilterOtherVisuals": True, "useEnhancedTooltips": True},
        },
        "definition/pages/pages.json": {"$schema": S_PAGES, "pageOrder": [p.name for p in pages],
                                        "activePageName": pages[0].name},
        f"StaticResources/RegisteredResources/{THEME_FILE}": theme(),
    }
    for p in pages:
        files[f"definition/pages/{p.name}/page.json"] = p.page_json()
        for v in p.visuals:
            files[f"definition/pages/{p.name}/visuals/{v['name']}/visual.json"] = v
    return files


def static_files() -> dict[str, str]:
    """Non-JSON registered resources (relative path inside ``<name>.Report/``) -> text: the wordmark SVG."""
    return {f"StaticResources/RegisteredResources/{WORDMARK}": WORDMARK_SRC.read_text(encoding="utf-8")}


def pbip(report_dir_name: str) -> dict:
    return {"$schema": S_PBIP, "version": "1.0", "artifacts": [{"report": {"path": report_dir_name}}],
            "settings": {"enableAutoRecovery": True}}


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
