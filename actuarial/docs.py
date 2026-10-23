"""Write the run's headline figures into ``docs/ACTUARIAL.md`` from ``actuarial.run_summary``.

The doc quotes IBNR, runoff, forecast and premium-deficiency figures. Typing them by hand let them go
stale after every model change, so they live in one generated block between two markers::

    <!-- run_summary:start -->
    ...
    <!-- run_summary:end -->

``python -m actuarial --write-docs`` rewrites that block after the run; nothing else in the doc changes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

START = "<!-- run_summary:start -->"
END = "<!-- run_summary:end -->"
DOC = Path(__file__).resolve().parents[1] / "docs" / "ACTUARIAL.md"


def _f(s: Mapping[str, object], k: str) -> float:
    try:
        return float(s[k])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def _money(v: float) -> str:
    if not math.isfinite(v):
        return "n/a"
    a = abs(v)
    txt = f"${a / 1e6:,.2f}M" if a >= 1e6 else f"${a / 1e3:,.1f}K" if a >= 1e3 else f"${a:,.0f}"
    return ("-" if v < 0 else "") + txt


def _pct(v: float, d: int = 1) -> str:
    return f"{100 * v:.{d}f}%" if math.isfinite(v) else "n/a"


def figures_block(s: Mapping[str, object], forecast: Mapping[str, float] | None = None) -> str:
    """The generated markdown block (markers included) for one run_summary (metric -> value)."""
    ibnr, rep = _f(s, "ibnr_selected_all"), _f(s, "reported_all")
    rows = [
        ("Valuation date (as of)", f"{s.get('valuation_date', 'n/a')} ({s.get('as_of', 'n/a')})"),
        ("Development factors fitted on diagonals from", str(s.get("factor_start_quarter", "all"))),
        ("Accident-quarter age-1 to age-2 factor", f"{_f(s, 'accident_link_age1'):.3f}"),
        ("Selected IBNR (true IBNR, all sources)", f"{_money(ibnr)} on {_money(rep)} reported "
                                                   f"({_pct(ibnr / rep if rep else float('nan'), 2)})"),
        ("Accident-quarter IBNR (before the cohort allocation) / on Admin Core lines",
         f"{_money(_f(s, 'ibnr_accident_quarter_basis'))} / {_money(_f(s, 'ibnr_source_admin_core'))}"),
        ("IBNR in days of incurred losses", f"{_f(s, 'ibnr_days_of_incurred'):.1f} days"),
        ("Report-lag IBNR: unreported claims / development on known claims / quarterly chain ladder (comparison)",
         f"{_money(_f(s, 'ibnr_unreported'))} / {_money(_f(s, 'ibnr_known_claim_development'))} / "
         f"{_money(_f(s, 'ibnr_chain_ladder_accident_quarter'))}"),
        ("Unreported claims: claims per day x mean report lag x severity",
         f"{_f(s, 'ibnr_lag_claims_per_day'):.1f} x {_f(s, 'ibnr_lag_mean_days'):.2f} days x "
         f"{_money(_f(s, 'ibnr_lag_severity'))}"),
        ("IBNR back-test, 12 earlier quarter-ends: mean absolute error / mean realized / total estimate to realized",
         f"{_money(_f(s, 'ibnr_backtest_mae'))} / {_money(_f(s, 'ibnr_backtest_mean_realized'))} "
         f"({_pct(_f(s, 'ibnr_backtest_scaled_mae'), 0)}) / {_f(s, 'ibnr_backtest_bias'):.2f}x"),
        ("Report lag: mean / within 14 days / 30 days or more",
         f"{_f(s, 'report_lag_mean_days'):.1f} days / {_pct(_f(s, 'report_lag_share_within_14d'))} / "
         f"{_pct(_f(s, 'report_lag_share_30d_plus'))}"),
        (f"New claims reported in the {_f(s, 'ibnr_runoff_window_days'):.0f} days after valuation (IBNR back-test)",
         f"{_money(_f(s, 'ibnr_runoff_new_claims'))} = {_pct(_f(s, 'ibnr_runoff_ratio'), 0)} of IBNR, against "
         f"{_pct(_f(s, 'ibnr_runoff_expected_share'), 0)} expected by now (A/E to date "
         f"{_f(s, 'ibnr_runoff_ae_to_date'):.2f}, {s.get('ibnr_runoff_flag', 'n/a')})"),
        ("Case-reserve development on claims known at valuation (not IBNR)",
         f"{_money(_f(s, 'case_development_known_claims'))} (denials {_money(_f(s, 'case_development_known_denied'))}, "
         f"other {_money(_f(s, 'case_development_known_other'))})"),
        ("Selected ultimate: sum of segments / pooled-triangle diagnostic",
         f"{_money(_f(s, 'selected_ultimate_sum_of_segments'))} / "
         f"{_money(_f(s, 'selected_ultimate_pooled_triangle'))}"),
        ("Claims outside the Claims-mart scope (not in the triangles)",
         f"no contract {_money(_f(s, 'incurred_lines_without_contract'))}; loss before the sale "
         f"{_money(_f(s, 'incurred_excluded_loss_before_sale'))} "
         f"({_f(s, 'claims_excluded_loss_before_sale'):.0f} claims)"),
        ("Losses on unexpired coverage (not IBNR)", _money(_f(s, "unexpired_risk_losses_all"))),
        ("Selected UPR, claims slice", f"{_money(_f(s, 'upr_selected'))}; unexpired losses are "
                                       f"{_pct(_f(s, 'unexpired_to_upr_ratio'), 0)} of it"),
        ("PDR needed (sum of segments)", _money(_f(s, "premium_deficiency_reserve_segments"))),
        ("Earned claims reserve to valuation (selected) / loss ratio",
         f"{_money(_f(s, 'earned_itd_selected_at_valuation'))} / {_pct(_f(s, 'loss_ratio_itd_at_valuation'))}"),
        ("Forecast model", str(s.get("forecast_model", "n/a")).replace("_", " ")),
        ("Last history quarter (developed) / next quarter forecast",
         f"{_pct(_f(s, 'forecast_last_history'))} / {_pct(_f(s, 'forecast_next_quarter'))}"),
    ]
    if forecast:
        rows.append(("Next-quarter band (about 95%)",
                     f"{_pct(forecast.get('lower', float('nan')))} to {_pct(forecast.get('upper', float('nan')))}"))
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (f"{START}\n_Generated by `python -m actuarial --write-docs` from `actuarial.run_summary`; "
            f"do not edit by hand._\n\n| Figure | This run |\n|---|---|\n{body}\n{END}")


def write_figures(s: Mapping[str, object], forecast: Mapping[str, float] | None = None, doc: Path = DOC) -> bool:
    """Replace the block in ``doc``. Returns False (and changes nothing) when the markers are missing."""
    text = doc.read_text(encoding="utf-8")
    a, b = text.find(START), text.find(END)
    if a < 0 or b < a:
        return False
    new = text[:a] + figures_block(s, forecast) + text[b + len(END):]
    if new != text:
        doc.write_text(new, encoding="utf-8", newline="\n")
    return True
