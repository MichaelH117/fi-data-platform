# Service levels (SLA / SLO)

An **SLA** is the promise to a stakeholder. An **SLO** is the internal target we track so we keep
the promise with room to spare. An **SLI** is the number we measure. All times are the business's
local time (`SLA_TIMEZONE`, default `America/Chicago`). Business days are Monday to Friday.

## The headline promise

> **Reports and dealer packets for business day D are published by 07:00 on D, built from data
> that passed every quality gate.** If the gates fail we publish nothing new: yesterday's site stays
> up with its "data as of" date, and stakeholders get a notice by 07:30.

Late is acceptable a few times a year. Wrong is not: no SLA trades correctness for speed.

## Service levels

| # | Service | SLI (how it is measured) | SLO (internal target) | SLA (promise) |
|---|---|---|---|---|
| 1 | Daily reports on time | A run whose `reports` step succeeded finished in the 24 h before 07:00 on D (`audit.pipeline_run`, shown on `site/ops.html`) | 95% of business days over a rolling 20 | 90% of business days per quarter |
| 2 | Pipeline duration | `audit.pipeline_run.seconds` for the scheduled run | p95 <= 5 min locally (design budget) | Run starts 05:00, so 2 h of slack before 07:00 |
| 3 | Data freshness | Latest `_loaded_at` per raw source (`diag_freshness`) | <= 26 h at 07:00 | <= 48 h, enforced by the gate |
| 4 | Gate pass rate | Share of scheduled runs where dbt, diagnostics and reconcile all pass | 98% | n/a (internal) |
| 5 | Reconciliation | `audit.reconciliation` breaks above $0.01 or 0 rows, after documented exclusions | 0 every run | 0: a break blocks publishing |
| 6 | Dealer packets | Packets built for every reportable dealer, each holding only its own rows | 100% each run | 100% on publish days |
| 7 | Incident response (Sev 1/2) | First response after the gate failure or report | 15 min (business hours) | 30 min |
| 8 | Stakeholder notice | Notice sent after a missed 07:00 | by 07:30 | by 08:00 |
| 9 | Ad-hoc requests | Intake triaged (owner + due date) | 2 business days | 3 business days |
| 10 | Metric correction | A wrong published number corrected and restated | 1 business day | 2 business days, with a change note |

## Severity

| Sev | Meaning | Example | Who is told |
|---|---|---|---|
| 1 | A wrong number reached an external audience (dealer, regulator, partner) | A filing used a stale reserve rate | Director, Senior Director, affected audience, same day |
| 2 | SLA missed, or a wrong number reached an internal audience | Gates failed, no 07:00 refresh | Report subscribers by 07:30; Director |
| 3 | Gate caught a problem before publishing, fixed inside the SLA | Diagnostics failed at 05:10, fixed and re-run by 06:30 | Team channel only |
| 4 | Cosmetic or documentation defect | A caption typo | Intake queue |

Every Sev 1 and Sev 2 gets a postmortem within 5 business days ([INCIDENT_TEMPLATE.md](INCIDENT_TEMPLATE.md)).

## Error budget

SLO 1 allows one missed day per 20 business days. When the rolling window has spent its budget, the
team freezes non-urgent releases ([RELEASE.md](RELEASE.md)) and spends the next sprint on reliability
work from the open postmortem actions.

## Where the numbers come from

| Source | Written by |
|---|---|
| `audit.pipeline_run` (one row per run, with each step's status and seconds) | `pipeline/run.py` |
| `audit.diagnostics_result` (one row per check per run, measured value and threshold) | `pipeline/diagnostics.py` |
| `audit.reconciliation` (one row per source x entity x comparison) | `pipeline/reconcile.py` |
| `data/runs/_gates.json` (last gate results; blocks `--from` / `--only` past a failed gate) | `pipeline/run.py` |
| `site/ops.html` (SLA calendar, gate and reconciliation tables, dealer packet list) | `reports/ops.py` |

This is a portfolio build with no scheduler running every morning, so the Operations page marks
business days before the first recorded run as *untracked* and leaves them out of the SLO rate.
