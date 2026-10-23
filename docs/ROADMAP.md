# Roadmap

Owner: Senior Director. This is the department's published backlog: the open gaps the platform's own outputs
show, ranked by **value** (which business number it protects), **risk** (what goes wrong if it waits) and
**effort**. Each item names the role that owns it (see section 3 of the [design contract](V3_DESIGN.md)).
The README's [Known limitations](../README.md#known-limitations) list the top items from this table.

Scale: value and risk are High / Medium / Low; effort is S (a day), M (a week), L (several weeks).

## Ranked backlog

| # | Item | Business metric affected | Value | Risk | Effort | Owner role | Status |
|---|---|---|---|---|---|---|---|
| 1 | **Convert the legacy in-force book into Admin Core.** At the 2023-01-01 cutover the legacy system still held 15,873 in-force contracts with $19.6M of coverage cost. They were never converted: they stay on the legacy system of record and load from it. The Operations page's reconciliation table shows them as `not_migrated` (row diff -15,873, amount diff -$19,603,459.28), and that row is excluded from the "Reconciliation breaks 0" headline. | Contracts in force, funded reserve on state filings, earned premium, claims-to-contract link for post-cutover claims on old contracts | High | High | L | Data Engineer (conversion load), Data Engineering Manager (`mart_migration_parallel_run`, reconcile) | Open. Disclosed as a documented exclusion. Done when the row reads TIED or TIED_AFTER_EXCLUSIONS |
| 2 | **Build up run-history and SLO evidence.** The 7 AM SLO table has no tracked days yet: every business day before the first recorded run is "Untracked", so the SLO tile reads n/a. | Reports published by 7 AM (SLO, target 95%) | High | Medium | S, then elapsed time | Reporting Manager (ops page, SLA), Data Engineering Manager (`pipeline/run.py` run log) | Open. `.\tasks.ps1 demo` fills a demo history; `.\tasks.ps1 schedule` registers the weekday 06:00 run that builds the real one |
| 3 | **Run one real UAT-to-prod promotion check.** `tools/env_diff.py` and the Operations page section that shows its result exist, but this build has no `data/env/env_diff.json`, because no UAT build has been diffed against prod yet. The section only appears after a check runs. | Release safety: no unexplained change to any mart's row count or money total | Medium | Medium | S | Data Engineering Manager (release), Reporting Manager ([RELEASE](operating-model/RELEASE.md)) | Open. Sample output is in RELEASE; a live run is pending |
| 4 | **One remittance late-fee rule.** `dealer.mart_remittance_aging` and `finance.mart_remittance_late_fees` both bill late fees with their own age bands ([METRICS](METRICS.md#open-governance-items)). | Dealer late fees billed, remittance aging | Medium | High (dealers can be billed two different amounts) | M | Senior Data Analyst (dealer & claims), Financial Analyst II | Open |
| 5 | **One combined-ratio formula.** The finance P&L uses the earned basis; the V1 executive overview used the written-expense basis. The P&L formula becomes the single `metrics.yml` entry. The cession loss ratio becomes a metric too. | Combined ratio, cession loss ratio | Medium | Medium | S | Senior Power BI Specialist (metrics), Financial Analyst II | Open |
| 6 | **Retire the V1 pages and compatibility views.** A few V1 report pages and semantic models still read the retired `gold` / `silver` names through `marts/core/v1_compat/`. | None directly; removes a second path to the same numbers | Medium | Low | M | Reporting Manager, Senior Power BI Specialist | Open |
| 7 | **Execute the server paths.** The Snowflake scripts, the dbt `snowflake` target, the Postgres RLS policies and the Airflow DAG are written and parse-tested, but they are not executed here (they need accounts or Docker). | Production readiness, dealer row-level security on the server | Medium | Medium | L | Data Engineer / Data Scientist | Open, by design for a local-only build |
| 8 | **Actuarial: fitted tail factor.** The development tail defaults to 1.0 ([ACTUARIAL](ACTUARIAL.md)). Fit a tail and show its effect next to the IBNR back-test. | IBNR, ultimate loss ratio | Medium | Low (IBNR is small: about $31K on this book) | M | Data Scientist | Open |
| 9 | **Faster reports and publish.** Reports and publish take about 87 of the run's 176 seconds, about half of the run. | Time to publish (the 7 AM SLO margin) | Low | Low | M | Reporting Manager, Data Engineering Manager | Open; the run is inside its 5-minute budget |
| 10 | **A hosted sample site.** The report site is build output and is not committed; the README shows [screenshots](img/) and CI keeps a `pipeline-output` artifact. A static-page deploy from CI would let a reader click through without a GitHub login. | Reviewer and stakeholder access to the reports | Low | Low | S | Data Engineer / Data Scientist (`.github/`) | Open |

## How the ranking works

1. Start from the business metric, not the tool. Items 1 to 3 rank first because a reader can see them on the
   Operations page today, and item 1 touches the in-force count and the funded reserve on every state filing.
2. Risk breaks ties: a wrong number sent to a dealer or a regulator (items 1 and 4) ranks above a slow page.
3. Effort only orders items of equal value and risk.

## One-page upward report (template)

Each release, the Senior Director sends one page up:

- **What changed:** the items closed since the last report, with the metric each one moved.
- **Why:** the value and risk from the table above.
- **Decision needed:** the one call that needs leadership. Today it is item 1: fund the conversion of the
  legacy in-force book, or formally keep the legacy system as the system of record for pre-cutover contracts
  and report them as a separate block.
