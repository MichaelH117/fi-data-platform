"""The ``F&I Data Platform`` report in PBIR format (one JSON file per page and per visual), generated.

Pages and visuals are declared here, on one layout grid, and written as PBIR JSON. PBIR is the format
Power BI Desktop saves a Power BI project (PBIP) in by default; it needs no preview feature in the
release this was built with (2.159). Every field a visual uses is checked against the semantic model
by ``powerbi.validate``.

Grid: 1280 x 720 page, 24 px margin, 12 columns of 88 px with 16 px gutters. Rows: header (title +
subtitle), filters, KPI cards, then two chart rows.
"""

from __future__ import annotations

import hashlib
import json

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

W, H, M, G, COLS = 1280, 720, 24, 16, 12
COL_W = (W - 2 * M - (COLS - 1) * G) // COLS  # 88
ROW_HEADER, ROW_FILTERS, ROW_KPI, ROW_A, ROW_B = (8, 60), (76, 60), (144, 88), (244, 226), (482, 226)

# Site palette (reports/static/site.css, light).
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#6f6d68"
PAGE_BG, SURFACE, BORDER, GRID = "#f6f6f3", "#fcfcfb", "#d9d8d0", "#e1e0d9"
ACCENT = "#1c5cab"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
CRITICAL_SOFT, WARNING_SOFT, GOOD_SOFT = "#f8dede", "#fdf0d2", "#dff2df"

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
    def chrome(title: str | None, tip: str | None, *, border: bool = True, pad: int = 8) -> dict:
        o: dict = {
            "background": [{"properties": {"show": lit(True), "color": color(SURFACE), "transparency": lit(0)}}],
            "border": [{"properties": {"show": lit(border), "color": color(BORDER), "radius": lit(10)}}],
            "dropShadow": [{"properties": {"show": lit(False)}}],
            "padding": [{"properties": {"top": lit(pad), "bottom": lit(pad), "left": lit(12), "right": lit(12)}}],
        }
        if title:
            o["title"] = [{"properties": {"show": lit(True), "text": lit(title), "fontColor": color(INK),
                                          "fontSize": lit(11), "bold": lit(True)}}]
        else:
            o["title"] = [{"properties": {"show": lit(False)}}]
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
                 "border": [{"properties": {"show": lit(card), "color": color(BORDER), "radius": lit(10)}}],
                 "title": [{"properties": {"show": lit(False)}}]}}
        return self.add(key, pos, v)

    def header(self) -> None:
        self.text("header", box(0, 12, ROW_HEADER),
                  [(self.title, "18pt", INK, True), (self.subtitle, "10pt", INK_2, False)])

    # ---- visuals
    def card(self, key: str, pos: dict, f: dict, title: str, tip: str, *, item: str | None = "T12M",
             filters: list[dict] | None = None):
        v = {"visualType": "card",
             "query": {"queryState": {"Values": {"projections": [proj(f, title)]}}},
             "objects": {"labels": [{"properties": {"color": color(INK), "fontSize": lit(22)}}],
                         "categoryLabels": [{"properties": {"show": lit(False)}}]},
             "visualContainerObjects": self.chrome(f"{title} · {item}" if item else title, tip),
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
        v = {"visualType": vtype, "query": query, "visualContainerObjects": self.chrome(title, tip),
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
            objects={"values": [{"properties": {"fontColor": rule_color(cr, [(2, 1.0, "#b02a2a")])},
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
AXIS = {"gridlineColor": {"solid": {"color": GRID}}, "labelColor": {"solid": {"color": INK_2}}}


def theme() -> dict:
    return {
        "name": model.MODEL_NAME,
        "dataColors": SERIES,
        "foreground": INK, "foregroundNeutralSecondary": INK_2, "foregroundNeutralTertiary": INK_3,
        "background": SURFACE, "backgroundLight": PAGE_BG, "backgroundNeutral": BORDER,
        "tableAccent": ACCENT, "accent": ACCENT,
        "good": GOOD, "neutral": WARNING, "bad": CRITICAL,
        "maximum": CRITICAL, "center": WARNING, "minimum": SURFACE, "null": GRID,
        "textClasses": {
            "title": {"fontFace": "Segoe UI Semibold", "fontSize": 11, "color": INK},
            "header": {"fontFace": "Segoe UI Semibold", "fontSize": 11, "color": INK},
            "label": {"fontFace": "Segoe UI", "fontSize": 9, "color": INK_2},
            "callout": {"fontFace": "Segoe UI Semibold", "fontSize": 22, "color": INK},
        },
        "visualStyles": {
            "*": {"*": {
                "background": [{"show": True, "color": {"solid": {"color": SURFACE}}, "transparency": 0}],
                "border": [{"show": True, "color": {"solid": {"color": BORDER}}, "radius": 10}],
                "dropShadow": [{"show": False}],
                "title": [{"fontColor": {"solid": {"color": INK}}, "fontSize": 11, "bold": True}],
                "categoryAxis": [AXIS],
                "valueAxis": [AXIS],
            }},
            "page": {"*": {
                "background": [{"color": {"solid": {"color": PAGE_BG}}, "transparency": 0}],
                "outspace": [{"color": {"solid": {"color": PAGE_BG}}}],
            }},
        },
    }


THEME_FILE = "FI_Data_Platform_Theme.json"


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
                                  "items": [{"name": THEME_FILE, "path": THEME_FILE, "type": "CustomTheme"}]}],
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


def pbip(report_dir_name: str) -> dict:
    return {"$schema": S_PBIP, "version": "1.0", "artifacts": [{"report": {"path": report_dir_name}}],
            "settings": {"enableAutoRecovery": True}}


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
