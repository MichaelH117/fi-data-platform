"""Plain-English explanations and glossary for the report site (docs/V3_DESIGN.md section 10.2).

One source, read once per build: ``dbt/seeds/core/metric_explanations.csv`` (three lines per metric plus the
watch/alert thresholds that set the status word) and ``dbt/seeds/core/glossary_terms.csv``. Pages never write
their own explanation text; a tile or chart names its ``metric_id`` and the template asks this module.

While a seed is not written yet, :func:`load` still returns a catalog: tiles show one muted "explanation
being written" line and the glossary lists the required terms with a placeholder, so the build never shows
an empty box and never invents a definition.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from markupsafe import Markup, escape

SEEDS = Path(__file__).resolve().parents[1] / "dbt" / "seeds" / "core"
TILE_MAP = Path(__file__).resolve().parent / "content" / "tile_metric_map.csv"  # label -> metric_id (RM-owned)
EXPLANATIONS_CSV = "metric_explanations.csv"
GLOSSARY_CSV = "glossary_terms.csv"
ROLES = ("dealer", "finance", "compliance", "executive")
STATUS_WORDS = {"good": "On track", "warning": "Watch", "critical": "Needs action"}
MAX_LINE = 160

# The minimum glossary set (section 10.2). Used only until glossary_terms.csv exists: names, no definitions.
REQUIRED_TERMS = [
    ("vsc", "VSC"), ("gap", "GAP"), ("ancillary", "Ancillary"), ("earned_vs_written", "Earned vs written"),
    ("loss_ratio", "Loss ratio"), ("cession", "Cession"), ("funded_reserve", "Funded reserve"), ("clip", "CLIP"),
    ("ibnr", "IBNR"), ("rule_of_78", "Rule of 78"), ("remit", "Remit"), ("chargeback", "Chargeback"),
]
# Extra spellings the auto-linker matches (besides the term, its parenthetical and simple plurals).
ALIASES = {"remit": [r"remit(?:s|ted|tance|tances)?"], "funded_reserve": [r"funded-reserve"]}
NO_LINK = {"earned_vs_written", "producer_vs_provider", "in_force", "pending", "seasoned", "tenant", "exposure"}


@dataclass(frozen=True)
class Explanation:
    metric_id: str
    plain_name: str = ""
    what_it_means: str = ""
    why_shown_this_way: str = ""
    why_your_boss_asks: str = ""
    good_direction: str = ""
    compare_to: str = ""
    audience_roles: tuple[str, ...] = ()
    decision_it_drives: str = ""
    watch_threshold: float | None = None
    alert_threshold: float | None = None
    placeholder: bool = False

    def lines(self) -> list[tuple[str, str]]:
        return [("What this means", self.what_it_means), ("Why it is shown this way", self.why_shown_this_way),
                ("Why your boss asks for this", self.why_your_boss_asks)]


@dataclass(frozen=True)
class Term:
    term_id: str
    term: str
    plain_definition: str = ""
    example: str = ""
    see_also: str = ""
    placeholder: bool = False


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(fh)]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class Catalog:
    metrics: dict[str, Explanation] = field(default_factory=dict)
    terms: list[Term] = field(default_factory=list)
    metrics_ready: bool = False
    terms_ready: bool = False
    tile_map: list[tuple[str, re.Pattern, str]] = field(default_factory=list)  # (kind, label pattern, metric_id)

    def resolve(self, kind: str, label: str) -> str:
        """metric_id for a tile or section label from the tile map ("" when unmapped or a guide section)."""
        exact = [m for k, rx, m in self.tile_map if k == kind and "*" not in rx.pattern and rx.fullmatch(label or "")]
        loose = [m for k, rx, m in self.tile_map if k == kind and rx.fullmatch(label or "")]
        return (exact or loose or [""])[0]

    def explain(self, metric_id: str = "", label: str = "") -> Explanation | None:
        """The seed row for ``metric_id`` (or a tile whose label equals a row's ``plain_name``).

        A named metric without a row gets a placeholder; an unnamed tile with no label match gets None.
        """
        if metric_id and metric_id in self.metrics:
            return self.metrics[metric_id]
        if label:
            key = label.strip().lower()
            for e in self.metrics.values():
                if e.plain_name and e.plain_name.lower() == key:
                    return e
        return Explanation(metric_id, placeholder=True) if metric_id else None

    def status(self, metric_id: str, value: float | None) -> str:
        """good / warning / critical from the seed thresholds; "" when the seed gives no rule or no value."""
        e = self.metrics.get(metric_id)
        if e is None or value is None or e.watch_threshold is None or e.good_direction not in ("up", "down"):
            return ""
        alert = e.alert_threshold
        if e.good_direction == "down":
            return "critical" if alert is not None and value > alert else (
                "warning" if value > e.watch_threshold else "good")
        return "critical" if alert is not None and value < alert else (
            "warning" if value < e.watch_threshold else "good")

    def for_role(self, role: str) -> list[str]:
        return [m for m, e in self.metrics.items() if role in e.audience_roles]

    def pick_for_role(self, role: str, starters: list[str], available: set[str], n: int = 4) -> list[str]:
        """Up to ``n`` tiles for a role page: the starter list first, then any other tile the seed tags for
        the role. Once the seed exists, a starter the seed does not tag for this role is dropped."""
        tagged = self.for_role(role)
        pool = [m for m in starters if not self.metrics_ready or m in tagged] + tagged
        out: list[str] = []
        for m in pool:
            if m in available and m not in out:
                out.append(m)
        return out[:n]

    def linker(self, root: str) -> Linker:
        return Linker(self.terms, root)


def load(seeds: Path = SEEDS, tile_map: Path = TILE_MAP) -> Catalog:
    cat = Catalog()
    for r in _rows(seeds / EXPLANATIONS_CSV):
        if not r.get("metric_id"):
            continue
        roles = tuple(x.strip() for x in r.get("audience_roles", "").split("|") if x.strip())
        cat.metrics[r["metric_id"]] = Explanation(
            r["metric_id"], r.get("plain_name", ""), r.get("what_it_means", ""), r.get("why_shown_this_way", ""),
            r.get("why_your_boss_asks", ""), r.get("good_direction", ""), r.get("compare_to", ""), roles,
            r.get("decision_it_drives", ""), _num(r.get("watch_threshold")), _num(r.get("alert_threshold")))
    cat.metrics_ready = bool(cat.metrics)
    terms = [Term(r.get("term_id") or slug(r["term"]), r["term"], r.get("plain_definition", ""),
                  r.get("example", ""), r.get("see_also", ""))
             for r in _rows(seeds / GLOSSARY_CSV) if r.get("term")]
    cat.terms_ready = bool(terms)
    cat.terms = terms or [Term(tid, name, placeholder=True) for tid, name in REQUIRED_TERMS]
    for r in _rows(tile_map):
        pattern = r.get("label_pattern", "")
        # a pattern with almost no fixed text ("*", "* (*)") would match any label on any page: skip it
        if r.get("kind") and r.get("metric_id") and len(pattern.replace("*", "").strip(" ,:()-")) >= 3:
            rx = re.compile(".*".join(re.escape(part) for part in pattern.split("*")))
            cat.tile_map.append((r["kind"], rx, r["metric_id"]))
    return cat


class Linker:
    """Turns the first use of each glossary term on one page into a link to ``glossary.html#<term_id>``.

    Create one per page (it remembers what it already linked). Input is plain text; output is escaped
    Markup, so authors never hand-write a link. Acronyms (all capitals) match case-sensitively.
    """

    def __init__(self, terms: list[Term], root: str) -> None:
        self.root = root
        self.seen: set[str] = set()
        forms: list[tuple[str, str]] = []  # (spoken form, term_id)
        for t in terms:
            if t.term_id in NO_LINK:
                continue
            head, _, paren = t.term.partition(" (")
            for form in (head.strip(), paren.rstrip(")").strip()):
                if len(form) >= 3 or (form and form.isupper()):
                    forms.append((form, t.term_id))
        parts, self.ids = [], []
        for form, tid in sorted(forms, key=lambda f: -len(f[0])):  # longest first: "loss ratio" before "loss"
            acronym = form.isupper()
            body = re.escape(form) + ("s?" if acronym else "(?:s|es)?")
            parts.append(f"(?P<t{len(self.ids)}>{body})" if acronym else f"(?P<t{len(self.ids)}>(?i:{body}))")
            self.ids.append(tid)
        for tid, extra in ALIASES.items():
            if any(t.term_id == tid for t in terms):
                parts.append(f"(?P<t{len(self.ids)}>(?i:{'|'.join(extra)}))")
                self.ids.append(tid)
        self.rx = re.compile(r"\b(?:" + "|".join(parts) + r")\b") if parts else None

    def __call__(self, text: str | None) -> Markup:
        if not text:
            return Markup("")
        safe = str(escape(text))
        if self.rx is None:
            return Markup(safe)

        def sub(m: re.Match) -> str:
            tid = self.ids[int(m.lastgroup[1:])]
            if tid in self.seen:
                return m.group(0)
            self.seen.add(tid)
            return f'<a class="gloss" href="{self.root}glossary.html#{tid}">{m.group(0)}</a>'

        return Markup(self.rx.sub(sub, safe))
