"""Guide pages for non-technical readers (docs/V3_DESIGN.md 10.3): role start pages, glossary, how the data
flows, and the definitions log.

These pages compute no numbers. Role pages come from one file, ``reports/content/role_pages.yml`` (button,
title, lede, tiles and pages); they reuse KPI tiles already built for other pages (the YAML tile list, refined
by the ``audience_roles`` column of the explanation seed).
Markup is built here from escaped text only; nothing comes from a mart.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from markupsafe import escape

from reports.explain import Catalog

ROOT = Path(__file__).resolve().parents[1]
METRICS_MD = ROOT / "docs" / "METRICS.md"
GLOSSARY = "glossary.html"
FLOW = "how-data-flows.html"
DEFINITIONS = "definitions.html"
GUIDE_NAV = [(GLOSSARY, "Glossary"), (FLOW, "How the data flows"), (DEFINITIONS, "Definitions decided")]


ROLE_PAGES_YML = ROOT / "reports" / "content" / "role_pages.yml"  # the one source for role pages (RM copy)


@dataclass(frozen=True)
class Role:
    key: str
    kicker: str  # "I'm a" / "I'm in" / "I'm an", split from the YAML button text
    name: str
    title: str
    what: str
    links: tuple[tuple[str, str, str], ...]  # (page, name, why you would open it); a page not built is skipped
    starter_tiles: tuple[str, ...]  # metric ids; the explanation seed's audience_roles can add or drop tiles


_BUTTON = re.compile(r"^(I'm (?:an|a|in))\s+(.+)$")


def load_roles(path: Path = ROLE_PAGES_YML) -> list[Role]:
    """Role start pages from reports/content/role_pages.yml (button, title, lede, tiles, pages)."""
    import yaml

    roles = []
    for r in yaml.safe_load(path.read_text(encoding="utf-8"))["roles"]:
        m = _BUTTON.match(r["button"].strip())
        kicker, name = (m.group(1), m.group(2)) if m else ("", r["button"].strip())
        roles.append(Role(r["id"], kicker, name[:1].upper() + name[1:], r["title"], " ".join(r["lede"].split()),
                          tuple((p["href"], p.get("name", ""), p["why"]) for p in r["pages"]),
                          tuple(r.get("tiles", ()))))
    return roles


ROLES = load_roles()


def role_path(role: Role) -> str:
    return f"roles/{role.key}.html"


def role_picker(prefix: str = "") -> str:
    """Four big buttons, one per role (used on the Start here page)."""
    return '<div class="roles">' + "".join(
        f'<a class="role-card" href="{prefix}{role_path(r)}"><span class="role-kicker">{escape(r.kicker)}</span>'
        f'<span class="role-name">{escape(r.name)}</span><span class="role-what">{escape(r.what)}</span>'
        f'<span class="role-go" aria-hidden="true">Open my pages &rarr;</span></a>' for r in ROLES) + "</div>"


def role_pages(page_cls, section_cls, built: set[str], tiles: dict, cat: Catalog, start: str) -> list:
    pages = []
    for r in ROLES:
        links = [(h, n, why) for h, n, why in r.links if h in built or h in (FLOW, DEFINITIONS)][:4]
        cards = "".join(
            f'<li><a class="role-card" href="../{escape(h)}"><span class="role-name">'
            f"{escape(n or _title_of(h, tiles))}</span><span class=\"role-what\">{escape(why)}</span>"
            f'<span class="role-go" aria-hidden="true">Open &rarr;</span></a></li>' for h, n, why in links)
        kpis = [copy.copy(tiles[m]) for m in cat.pick_for_role(r.key, list(r.starter_tiles), set(tiles))]
        pages.append(page_cls(
            role_path(r), r.title, r.what, kpis,
            [section_cls("Your pages", "Start with these. Each one opens in this site; the menu on the left has "
                         "everything else.", html=f'<ul class="role-links">{cards}</ul>', wide=True)],
            nav=start, crumb="Start here"))
    return pages


_TITLES: dict[str, str] = {}


def _title_of(href: str, _tiles: dict) -> str:
    return _TITLES.get(href, href.rsplit("/", 1)[-1].replace(".html", "").replace("-", " ").capitalize())


def remember_titles(pages: Sequence) -> None:
    _TITLES.update({p.path: p.title for p in pages})
    _TITLES.update({GLOSSARY: "Glossary", FLOW: "How the data flows", DEFINITIONS: "Definitions decided"})


# --------------------------------------------------------------------------- glossary
def page_glossary(page_cls, section_cls, cat: Catalog):
    terms = sorted(cat.terms, key=lambda t: t.term.lower())
    names = {t.term_id: t.term.split(" (")[0] for t in terms}
    first: dict[str, str] = {}  # letter -> first term id, for the A-Z jump row
    for t in terms:
        first.setdefault(t.term[:1].upper(), t.term_id)
    az = "".join(f'<li><a href="#{escape(tid)}" aria-label="Words starting with {escape(letter)}">'
                 f'{escape(letter)}</a></li>' for letter, tid in first.items())
    items = []
    for t in terms:
        if t.placeholder or not t.plain_definition:
            body = '<p class="pending">Plain definition is being written.</p>'
        else:
            body = f"<p>{escape(t.plain_definition)}</p>"
            if t.example:
                body += f'<p><span class="term-label">Example</span>{escape(t.example)}</p>'
            refs = [r for r in t.see_also.replace(",", "|").split("|") if r.strip()]
            if refs:
                body += ('<p><span class="term-label">See also</span>' + ", ".join(
                    f'<a href="#{escape(r.strip())}">{escape(names.get(r.strip(), r.strip()))}</a>' for r in refs)
                    + "</p>")
        items.append(f'<li class="term" id="{escape(t.term_id)}"><h3>{escape(t.term)}</h3>{body}</li>')
    return page_cls(
        GLOSSARY, "Glossary",
        "The words this site uses, in plain English. Wherever one of these words first appears on a page, it is "
        "underlined with dots and links here.", [],
        [section_cls("Every word, A to Z", "Jump to a letter, or scroll. Each word has a plain meaning and an "
                     "example.", html=f'<ul class="az">{az}</ul><ul class="terms">{"".join(items)}</ul>', wide=True)])


# --------------------------------------------------------------------------- how the data flows
FLOW_STEPS = [  # (title, what happens, what can go wrong, how we stop it, page that shows the check)
    ("Dealer sells a contract",
     "A customer buys a car and adds a service contract. The dealer sends the contract and the money in.",
     "Details are missing or mistyped, or the money arrives late.",
     "Every required field is checked on arrival, and late money shows up on the remittance pages.",
     "dealer/remittance-aging.html"),
    ("Admin system records it",
     "The contract, its payments, claims and cancellations are stored in the administration system.",
     "A cancel is entered twice, or a code quietly changes meaning.",
     "Row counts and dollar totals are captured at the source before anything moves.",
     "ops.html"),
    ("Warehouse organizes it",
     "Copies land in the warehouse untouched, then are cleaned into one consistent model.",
     "Rows are dropped or doubled while loading, or personal details travel too far.",
     "Counts and dollars are tied out from source to warehouse; personal details are hashed or dropped early.",
     "ops.html"),
    ("Checks test it",
     "Automated tests and diagnostics gates run on every build, before anyone sees a number.",
     "A number looks fine but breaks a rule, like parts that do not add up to their total.",
     "A failed check stops publishing. Yesterday's reports stay up until the problem is fixed.",
     "quality.html"),
    ("Reports show it",
     "This site, the Excel downloads, the dealer packets and the state filings.",
     "Two pages show different numbers for the same thing, or a dealer sees another dealer's rows.",
     "Each number has one definition used everywhere, and each dealer packet is filtered to its own rows.",
     "semantic.html"),
]


def page_flow(page_cls, section_cls, built: set[str]):
    steps = []
    for i, (title, what, risk, fix, href) in enumerate(FLOW_STEPS, 1):
        see = f' <a href="{escape(href)}">See the check</a>' if href in built else ""
        steps.append(
            f'<li class="flow-step"><span class="flow-num">Step {i:02d}</span>'
            f'<h3 class="flow-title">{escape(title)}</h3>'
            f'<p class="flow-what">{escape(what)}</p>'
            f'<p class="flow-risk"><b>What can go wrong</b>{escape(risk)}</p>'
            f'<p class="flow-fix"><b>How we stop it</b>{escape(fix)}{see}</p></li>')
    legacy = ('<p>The old warehouse skipped most of these checks, so mistakes reached the reports. '
              '<a href="legacy.html">Legacy vs better</a> shows the same questions answered both ways.</p>'
              if "legacy.html" in built else "")
    return page_cls(
        FLOW, "How the data flows",
        "Five steps from a dealer's sale to the report you are reading, what can go wrong at each one, and how "
        "it is stopped.", [],
        [section_cls("From sale to report", "Read left to right. The moving light shows the direction the data "
                     "travels.", html=f'<ol class="flow">{"".join(steps)}</ol>', wide=True),
         *([section_cls("Why this matters", "", html=legacy, wide=True)] if legacy else [])])


# --------------------------------------------------------------------------- definitions decided
def definitions_rows(md: Path = METRICS_MD) -> tuple[list[str], list[list[str]]]:
    """The first Markdown table under a '## Definitions decided' heading in docs/METRICS.md (or nothing)."""
    if not md.is_file():
        return [], []
    text = md.read_text(encoding="utf-8")
    m = re.search(r"^##\s+Definitions decided.*?$(.*?)(?=^##\s|\Z)", text, flags=re.M | re.S)
    if not m:
        return [], []
    rows = [ln.strip().strip("|").split("|") for ln in m.group(1).splitlines() if ln.strip().startswith("|")]
    rows = [[c.strip() for c in r] for r in rows if not all(set(c.strip()) <= set("-: ") for c in r)]
    return (rows[0], rows[1:]) if rows else ([], [])


def page_definitions(page_cls, section_cls, md: Path = METRICS_MD):
    cols, rows = definitions_rows(md)
    lede = ("Words that used to mean different things to different teams, the meaning the team agreed on, "
            "who owns it and when it was decided.")
    if not rows:
        return page_cls(DEFINITIONS, "Definitions decided", lede, [], [section_cls(
            "Decisions log", "", wide=True,
            html='<p class="placeholder-box">The decisions log is being written. It will list each term, the '
                 "agreed meaning, the owner, the date and what it replaced.</p>")])
    # One card per decision (a five-column table is too wide to read); code backticks are dropped for readers.
    def card(r: list[str]) -> str:
        cells = dict(zip(cols, (c.replace("`", "") for c in r), strict=False))
        lines = "".join(f'<p><span class="term-label">{escape(k)}</span>{escape(v)}</p>'
                        for k, v in cells.items() if k != cols[0] and v)
        return f'<li class="term"><h3>{escape(cells[cols[0]])}</h3>{lines}</li>'
    return page_cls(DEFINITIONS, "Definitions decided", lede, [], [section_cls(
        "Decisions log", "Newest decisions last. Source: docs/METRICS.md.", wide=True,
        html=f'<ul class="terms">{"".join(card(r) for r in rows)}</ul>')])
