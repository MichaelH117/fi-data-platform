# Analyst SQL playbook

These are the queries a senior analyst runs against the **legacy** warehouse (`vsc_legacy`, built by
`python -m legacy build`). Each file answers one business question and avoids one trap that the legacy
setup lays for you. Where it helps, a `*.modern.sql` sibling asks the same question on the modern marts,
so you can see the trap disappear.

All data is synthetic.

## How to run

```
python -m legacy build        # once: builds the legacy databases (needs the local PostgreSQL)
python -m legacy playbook     # runs every file in manifest.yml, checks the row counts, writes the table
```

The runner gives each file a hard time limit (from `manifest.yml`). It checks that the **last** result of
the file has a row count in the expected range. It prints a PASS / FAIL / SKIP table and writes it to
`data/legacy/playbook_results.md` and `.json`. A modern sibling shows SKIP when the modern marts are not
synced. Exit code: 0 when nothing failed, 1 when something failed, 2 when PostgreSQL is down.

## House style (every file)

- `set statement_timeout` is the first line, so a runaway query stops on its own.
- `explain` comes before every heavy query. You check the plan first, then run.
- SQL keywords are lowercase and `group by` is positional (`group by 1, 2`).
- Legacy columns are quoted CamelCase (`"ContractId"`, `"Reportable State"`), the way the old tables spell them.
- Discovery goes through `pg_catalog`, not `information_schema`.
- Each file starts with a header that names the pattern, the business question and the trap.

## The patterns, in plain words

| File | What it does | Why an analyst needs it |
|---|---|---|
| `01_catalog_discovery` | Lists every table and view, its estimated size, and whether you may read it. Finds which views carry a column like "vin". Lists the indexes. | You can't query what you can't find. This tells you in a second what exists, how big it is, and what you're allowed to read, before a query fails halfway through. |
| `02_freshness_probe` | Reads a small random sample (`tablesample`) of two big tables to see the newest date. | Before you build a report, check that last month's data has actually arrived. A full scan of 3 million rows just to check a date wastes minutes. |
| `03_diag_dim_match_by_year` (+ modern) | Counts, for each entered year, how many contracts find their product in the product table. | The product table stopped updating. With a normal (inner) join, this year's contracts quietly disappear and it looks like sales dropped. This count shows exactly which year breaks. |
| `04_diag_key_bridge_vin` | Matches the dealer stock list to contracts by VIN: first exactly, then after tidying the text (upper case, no spaces). | People type VINs by hand. The exact match loses about 1 row in 5. Tidying the VIN once and then joining fixes it, and it runs fast. Tidying row by row inside a lookup takes minutes. |
| `05_diag_claim_key_coverage` | For each source system, the share of claims whose contract id really points at a contract. Also checks whether claim numbers clash once the leading zeros are removed. | A claim without a contract can't be counted for a dealer or a product. Old claim numbers have leading zeros. Removing them can make two different claims look like one, so the key has to include the source system. |
| `06_diag_fanout_guard` | Counts rows before and after a join and shows how much money the duplicates add. | Some transactions were sent twice. A plain join copies those rows and inflates coverage cost by about $1M. Counting before and after every join is how you catch it. |
| `07_funded_reserve_filing` (+ modern) | Builds the state funded-reserve filing lines for one provider, by state: contracts written (all-time and this reporting period), contracts in force at period end, gross consideration on two bases, claims paid, net, and the 40% reserve. | "In force" by today's status is not "in force on Aug 31". Claims are stored as negative numbers. Demo dealers and test contracts have to come out first. A filing that misses any of these is wrong. |
| `08_claim_grain_export` | One row per claim, with status and substatus kept as a pair and the paid amount made positive. | A flat export that people slice in Excel later. The sign fix and the clear "no substatus" label stop someone from adding negative numbers or reading a blank as "unknown". |
| `09_cancel_export_tenant_exclusions` | Cancellations per tenant, after each tenant's own exclusion list (written as data, at the top of the query). | Every tenant has different things it leaves out of its report. Keeping those lists as visible data means one query serves every tenant. It reads the live table, not the old copied schema that is a month behind. |
| `10_coverage_cost_lineage_scoped` | Works out coverage cost the same way the reporting view does, but only for the contracts you need, and checks the result against the finance table. | The reporting view adds up every disbursement in the whole book before it applies your filter. Picking the contracts first gives the same number much faster. The diff column shows the number is still right. |
| `11_dealer_duplicate_identity` (+ modern) | Finds dealers that exist under more than one id (an inactive copy, a renamed number, a name with extra spaces) and counts the contracts on each. | If you filter on only one dealer id, you miss up to half of that dealer's contracts. Run this before any dealer report. |
| `12_eav_custom_field_pivot` | Turns the custom-field store (one row per field) into one row per RV contract with real columns. | Custom fields are stored as text, in a separate database. A field with no value has no row at all, so the pivot counts the missing fields instead of assuming every contract has all five. |
| `13_producer_vs_provider_trap` (+ modern) | Counts a provider's contracts three ways side by side. | In one layer "Producers" is the selling dealer's name. In another, "Producer" is the provider company. Filtering the wrong column returns 0, which looks like "no business" when it is really the wrong column. |
| `14_loss_ratio_earnings_sentinel` (+ modern) | Loss ratio (claims paid divided by earned reserve) by month and by entered-year group of contracts. | Converted contracts carry a fake date (1900-01-01) for their early months. Grouping by date makes a "January 1900" row, and a lazy date filter quietly drops that money. The file shows how big that slice is and handles it on purpose. |

## DAX measure to SQL (files 15 to 18)

These four take measures exactly as written in the generated legacy semantic models
(`legacy/semantic_models/`) and rewrite them in SQL. That's how you check a Power BI card against the
database. It also shows why the three legacy models disagree with each other.

| File | DAX measure(s) | The SQL idea | What it shows |
|---|---|---|---|
| `15_dax_in_force` | Ops `Contracts = COUNTROWS(...)`, `'In Force' = CALCULATE([Contracts], status = "INFORCE")`, `'Written Premium'`; Reporting `Contracts = DISTINCTCOUNT(...)` | The rows of a visual become the `group by`. `CALCULATE(measure, column = x)` becomes `count(*) filter (where column = x)`. `DISTINCTCOUNT` becomes `count(distinct ...)`. | Two models both have a measure called "Contracts", and both include test contracts. Neither one is a filing number. |
| `16_dax_claims_paid_sign` (+ modern) | Ops `'Claims Paid' = SUM(Claims[ClaimAmount])` and Reporting `'Claims Paid' = SUM('Fact Claims'[Claim Amount])` | `SUM` becomes `sum()` at the grain of the visual. | The same name has the opposite sign, so two reports show "Claims Paid" with different signs. The modern claim fact has one positive definition. |
| `17_dax_coverage_cost_gen2` | Finance `'Coverage Cost'` and `'Coverage Cost Gen2'` | Add up each fact to entered year on its own, then join on the year. Never join two facts row to row. | Gen2 stopped loading weeks ago and counts the re-sent transactions twice. The same word gives two answers. |
| `18_dax_cancel_refunds` | Ops `'Cancel Refunds'` and Finance Gen3 `'Cancel Refunds'` | A month axis becomes `date_trunc('month', ...)`. A row count next to the sum shows which rows were dropped. | Gen3 flips the sign and drops cancels with no refund, so a cancel rate built on it comes out too low. |

## Adding a query

1. Write `NN_name.sql` with the header (`-- pattern:`, `-- question:`, `-- trap:`) and `set statement_timeout` first.
2. Add it to `manifest.yml` with its database, timeout and an expected row range.
3. Run `python -m legacy playbook` and `pytest tests/test_playbook.py`.
