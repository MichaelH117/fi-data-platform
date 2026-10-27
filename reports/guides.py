"""Guide pages for non-technical readers (docs/V3_DESIGN.md 10.3): role start pages, glossary, how the data
flows, and the definitions log.

These pages compute no numbers. The department hubs (the Start page "Who are you?" grid and every
``roles/<id>.html``) come from one file, ``reports/content/role_pages.yml``: bands, questions, pages, glossary
terms, owner and tiles. Tiles reuse KPI tiles already built for other pages (the YAML tile list, refined by the
``audience_roles`` column of the explanation seed).
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


ROLE_PAGES_YML = ROOT / "reports" / "content" / "role_pages.yml"  # THE one source for the hubs (RM copy)


@dataclass(frozen=True)
class Question:
    ask: str
    why: str
    page: str  # the page (or download) that answers it; the link is dropped when that page is not built


@dataclass(frozen=True)
class Role:
    """One department hub (roles/<key>.html), read from role_pages.yml."""

    key: str
    kicker: str  # "I'm a" / "I'm in" / "I'm an", split from the YAML button text
    name: str
    title: str
    what: str
    links: tuple[tuple[str, str, str], ...]  # (page, name, why you would open it); a page not built is skipped
    starter_tiles: tuple[str, ...]  # metric ids; the explanation seed's audience_roles can add or drop tiles
    band: str = ""
    audience: str = ""  # the audience_roles tag (metric_explanations.csv) that refines the tiles
    questions: tuple[Question, ...] = ()
    terms: tuple[str, ...] = ()  # glossary term ids
    owner: str = ""
    check: str = ""
    check_name: str = ""
    runbook: str = ""


@dataclass(frozen=True)
class Hubs:
    intro: str
    bands: tuple[tuple[str, str], ...]  # (id, name), in display order
    runbook: str
    correction_rule: str
    roles: tuple[Role, ...]


_BUTTON = re.compile(r"^(I'm (?:an|a|in))\s+(.+)$")


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def load_hubs(path: Path = ROLE_PAGES_YML) -> Hubs:
    """Department hubs from reports/content/role_pages.yml: bands, questions, pages, terms, owner, tiles."""
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    roles = []
    for r in doc["roles"]:
        m = _BUTTON.match(r["button"].strip())
        kicker, name = (m.group(1), m.group(2)) if m else ("", r["button"].strip())
        wrong = r.get("if_wrong") or {}
        roles.append(Role(
            r["id"], kicker, name[:1].upper() + name[1:], r["title"], _flat(r["lede"]),
            tuple((p["href"], p.get("name", ""), _flat(p["why"])) for p in r["pages"]),
            tuple(r.get("tiles", ())), r.get("band", ""), r.get("audience", r["id"]),
            tuple(Question(_flat(q["ask"]), _flat(q["why"]), q.get("page", "")) for q in r.get("questions", ())),
            tuple(r.get("terms", ())), wrong.get("owner", ""), wrong.get("check", ""),
            wrong.get("check_name", ""), wrong.get("runbook", "")))
    bands = tuple((b["id"], b["name"]) for b in doc.get("bands", ()))
    return Hubs(_flat(doc.get("intro")), bands, doc.get("runbook", ""), _flat(doc.get("correction_rule")),
                tuple(roles))


def load_roles(path: Path = ROLE_PAGES_YML) -> list[Role]:
    return list(load_hubs(path).roles)


HUBS = load_hubs()
ROLES = list(HUBS.roles)  # the hubs, in YAML order (kept under this name for build.py and the tests)


def role_path(role: Role) -> str:
    return f"roles/{role.key}.html"


def _card(r: Role, prefix: str, band: str = "") -> str:
    n_q, n_p = len(r.questions), len(r.links)
    count = (f'<span class="hub-count">{n_q} question{"s" * (n_q != 1)} &middot; {n_p} '
             f'page{"s" * (n_p != 1)}</span>') if n_q else ""
    kicker = band or r.kicker
    return (f'<a class="role-card" href="{prefix}{role_path(r)}"><span class="role-kicker">{escape(kicker)}</span>'
            f'<span class="role-name">{escape(r.name)}</span><span class="role-what">{escape(r.what)}</span>'
            f'<span class="role-go">{count}<span aria-hidden="true">Open the hub &rarr;</span></span></a>')


def role_picker(prefix: str = "") -> str:
    """The "Who are you?" grid on Start here: every hub in one grid, ordered by the YAML's bands; each card
    carries its band name as the small kicker."""
    names = dict(HUBS.bands)
    rank = {b: i for i, (b, _) in enumerate(HUBS.bands)}
    hubs = sorted(ROLES, key=lambda r: rank.get(r.band, len(rank)))  # stable: YAML order inside a band
    return '<div class="roles hubs">' + "".join(_card(r, prefix, names.get(r.band, "")) for r in hubs) + "</div>"


def _href(h: str) -> str:
    return h if h.startswith(("http:", "https:", "#")) else f"../{h}"


def role_pages(page_cls, section_cls, built: set[str], tiles: dict, cat: Catalog, start: str,
               out: Path | None = None) -> list:
    """One hub page per role: tiles, questions, the pages that answer them, key words, and who to ask."""
    def ok(h: str) -> bool:  # a report page that was built, or a file (an Excel download) that exists
        return bool(h) and (h in built or (out is not None and (out / h).is_file()))

    terms = {t.term_id: t for t in cat.terms}
    pages = []
    for r in ROLES:
        links = [(h, n, why) for h, n, why in r.links if ok(h)][:6]
        names = {h: n or _title_of(h, tiles) for h, n, _ in r.links}
        kpis = [copy.copy(tiles[m]) for m in cat.pick_for_role(r.audience, list(r.starter_tiles), set(tiles))]
        sections = []
        if r.questions:
            qs = "".join(
                f'<li class="hub-q"><span class="hub-q-num">Q{i:02d}</span><h3>{escape(q.ask)}</h3>'
                f"<p>{escape(q.why)}</p>"
                + (f'<p class="hub-q-ans"><a href="{escape(_href(q.page))}">Answered on: '
                   f"{escape(names.get(q.page) or _title_of(q.page, tiles))}</a></p>" if ok(q.page) else "")
                + "</li>" for i, q in enumerate(r.questions, 1))
            sections.append(section_cls(
                "Questions this team asks", "The questions that come up most, why they are asked, and where the "
                "answer lives.", html=f'<ol class="hub-qs">{qs}</ol>', wide=True))
        cards = "".join(
            f'<li><a class="role-card" href="{escape(_href(h))}"><span class="role-name">'
            f'{escape(names[h])}</span><span class="role-what">{escape(why)}</span>'
            f'<span class="role-go" aria-hidden="true">'
            f'{"Download" if h.endswith((".xlsx", ".csv")) else "Open"} &rarr;</span></a></li>'
            for h, n, why in links)
        sections.append(section_cls(
            "Pages that answer them", "Start with these. Pages open in this site; Excel files download. The menu "
            "on the left has everything else.", html=f'<ul class="role-links">{cards}</ul>', wide=True))
        words = [terms[t] for t in r.terms if t in terms]
        if words:
            items = "".join(
                f'<li><a href="../{GLOSSARY}#{escape(t.term_id)}"><b>{escape(t.term)}</b>'
                f"<span>{escape(_first_sentence(t.plain_definition))}</span></a></li>" for t in words)
            sections.append(section_cls(
                "Words this hub relies on", "Each one links to its plain-English entry in the glossary.",
                html=f'<ul class="hub-terms">{items}</ul>', wide=True))
        if r.owner:
            check = (f'<a href="{escape(_href(r.check))}">{escape(r.check_name or _title_of(r.check, tiles))}</a>'
                     if ok(r.check) else "")
            steps = [
                "<li><b>Check the date.</b> Every page says what day its data is as of. A number from "
                "yesterday's site is old, not wrong.</li>",
                *([f"<li><b>Look at the check.</b> {check} shows the tests that ran on this data and whether "
                   "any of them failed.</li>"] if check else []),
                f"<li><b>Ask the owner.</b> These numbers belong to the <b>{escape(r.owner)}</b>. Send the page, "
                "the number and what you expected to see.</li>",
                *([f"<li><b>The fix is written down.</b> The owner follows <code>{escape(HUBS.runbook)}</code>, "
                   f"section <i>{escape(r.runbook)}</i>.</li>"] if r.runbook and HUBS.runbook else []),
            ]
            rule = f'<p class="hub-rule">{escape(HUBS.correction_rule)}</p>' if HUBS.correction_rule else ""
            sections.append(section_cls(
                "If a number looks wrong", f"{len(steps)} steps, in this order.",
                html=f'<ol class="hub-wrong">{"".join(steps)}</ol>{rule}', wide=True))
        pages.append(page_cls(role_path(r), r.title, r.what, kpis, sections, nav=start, crumb="Start here"))
    return pages


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text or "")
    return m.group(1) if m else (text or "")


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
    from reports import infographics  # local: infographics is a leaf module, kept out of import time

    figs = [(title, html) for title, html in (
        ("The checks, with this run's numbers", infographics.figure(
            "05-data-flow-gates", FLOW,
            "The same journey as a picture. Each green diamond is a check. The numbers under it are what that "
            "check did in the latest run. If checks 3, 4 or 5 fail, nothing new is published.")),
        ("The life of one contract", infographics.figure(
            "01-contract-life", FLOW,
            "What happens to a contract after the sale: it waits for the dealer's money, is covered while "
            "repairs are paid, then expires or is cancelled. The bar shows where every contract is today.")),
    ) if html]
    return page_cls(
        FLOW, "How the data flows",
        "Five steps from a dealer's sale to the report you are reading, what can go wrong at each one, and how "
        "it is stopped.", [],
        [section_cls("From sale to report", "Read left to right. The moving light shows the direction the data "
                     "travels.", html=f'<ol class="flow">{"".join(steps)}</ol>', wide=True),
         *[section_cls(title, "", html=html, wide=True) for title, html in figs],
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
