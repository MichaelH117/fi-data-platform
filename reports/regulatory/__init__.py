"""Regulatory reports: state funded-reserve / provider renewal filings and state market surveys.

Report plug-in (docs/V3_DESIGN.md section 3): the shell calls ``build(con, out_dir)``; this writes the
Excel workbooks to ``<out_dir>/downloads/regulatory/`` and returns the HTML pages. Reads the
``regulatory`` marts and the regulatory seeds only.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from reports.build import Page
from reports.regulatory import data, excel, pages

NAV_LABEL = "Regulatory"
DOWNLOADS = "downloads/regulatory"


def build(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Page]:
    if not data.available(con):
        return []
    d = data.load(con)
    names = d.methods.set_index("state")["state_name"]
    files: dict[str, str] = {}
    out: list[Page] = []
    for state in sorted(d.summary.state.unique()):
        s = d.summary[d.summary.state == state]
        end = s.period_end.max()
        rel = f"{DOWNLOADS}/funded_reserve_{state.lower()}_{end:%Y%m%d}.xlsx"
        tabs = {
            "ITD Contracts": data.filing_contracts(con, state, "ITD").rename(columns=data.CONTRACT_COLS),
            "Period Contracts": data.filing_contracts(con, state, "PERIOD").rename(columns=data.CONTRACT_COLS),
            "ITD Claims": data.filing_claims(con, state, "ITD").rename(columns=data.CLAIM_COLS),
            "Period Claims": data.filing_claims(con, state, "PERIOD").rename(columns=data.CLAIM_COLS),
        }
        excel.write_filing(out_dir / rel, state, names[state], s, d.definitions, tabs, d.as_of)
        files[f"filing:{state}"] = rel
        out.append(pages.filing_page(d, state, rel))
    for state in sorted(d.survey.state.unique()):
        s = d.survey[d.survey.state == state]
        rel = f"{DOWNLOADS}/market_survey_{state.lower()}_{s.calendar_year.max()}.xlsx"
        details = data.survey_details(con, state, s.year_start.min().date(), s.year_end.max().date())
        excel.write_survey(out_dir / rel, state, names[state], s, details, d.as_of, data.survey_notes(d.definitions))
        files[f"survey:{state}"] = rel
        out.append(pages.survey_page(d, state, rel))
    return [pages.index_page(d, files), *out, pages.definitions_page(d)]
