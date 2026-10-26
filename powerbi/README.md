# F&I Data Platform: Power BI report (PBIP)

A seven-page Power BI report over the warehouse on the local PostgreSQL server. It is saved as a
**Power BI project (PBIP)**: plain text files instead of one binary `.pbix`, so git can show every
change.

| Path | What it is |
|---|---|
| `FI Data Platform.pbip` | Double-click this to open the project in Power BI Desktop |
| `FI Data Platform.Report/` | The report in **PBIR** format: one JSON file per page and per visual, plus the theme |
| `FI Data Platform.SemanticModel/` | The reporting semantic model in **TMDL** (tables, relationships, measures, roles) |
| `build.py`, `report.py`, `model.py` | The generator. `python -m powerbi` rewrites the two folders and the `.pbip` |
| `validate.py`, `schemas.py` | Checks: Microsoft's JSON schemas, field references, layout grid |

The display name is **F&I Data Platform** (model, report theme, exports). Files and folders drop the
"&" (`FI Data Platform`) because it is unsafe in paths and URLs.

Pages: **Executive overview** (KPI cards, written vs earned trend, loss ratio), **Dealer scorecard**
(table with slicers and conditional formatting, RLS note), **Claims & IBNR** (frequency and
severity, aging, top components, IBNR by accident quarter), **Regulatory funded reserve** (state
and scope slicers, section 2 and 3 filed lines), **Finance** (P&L by product, cession and profit
share, MTD/QTD/YTD from the calculation group), **Experience** (a "View by" field parameter:
Date, Product, Dealer, State, Plan, Make, Entered year, Term; closed periods to date) and **New
Reserves** (claims reserve booked at sale by product category and term).

## Open it, step by step

1. **Start the database.** Postgres runs as your user, not as a Windows service, so start it
   again after every reboot. In PowerShell, in the project folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\pg.ps1 start
   powershell -ExecutionPolicy Bypass -File tools\pg.ps1 status   # "accepting connections"
   ```

2. **Open the project.** Double-click `powerbi\FI Data Platform.pbip`, or in Power BI Desktop use
   File > Open > Browse and pick the `.pbip` file. The pages open empty, with a yellow bar saying
   "Some of the tables have incomplete or no data". That is expected: the project stores no data,
   only the design.

3. **Refresh.** Click **Refresh now** in that bar (or Home > Refresh).

4. **Sign in when asked.** Power BI asks for credentials for `localhost:5432;vsc_analytics`:
   - Pick the **Database** tab (not Windows).
   - User name: `bi_reader`
   - Password: open the git-ignored file `.env` in the project root (Notepad is fine) and copy the
     value after `PG_BI_READER_PASSWORD=`. Never paste it into a file that git tracks.
   - "Select which level to apply these settings to": keep `localhost:5432`, then **Connect**.
   - If it then says it cannot connect with an **encrypted connection**, click **OK** to connect
     without encryption. The server only listens on your own machine (127.0.0.1), so nothing
     leaves the computer.

   Desktop remembers the login. To change it later: File > Options and settings > Data source
   settings > `localhost:5432;vsc_analytics` > Edit permissions.

5. **Wait for the load.** It imports about 13 million rows (the earned-disbursement table alone
   is about 8 million), so the first refresh takes several minutes and Desktop uses 1 to 3 GB of
   memory. When it finishes, every visual fills in.

6. **Test row-level security with "View as".** Modeling > **View as** > tick **Other user** and type
   a dealer login, tick **Dealer User**, then OK. To find a login:

   ```powershell
   "select user_login, dealer_number from core.dim_user_dealer_access limit 5;" | powershell -ExecutionPolicy Bypass -File tools\pg.ps1 psql
   ```

   The dealer scorecard now shows only that login's dealers, and the regulatory filing and IBNR
   visuals go blank (internal only). Try **Tenant Partner** with the same login to see the whole
   program. Click **Stop viewing** in the yellow bar to go back.

7. **Close the database when you are done** (optional):
   `powershell -ExecutionPolicy Bypass -File tools\pg.ps1 stop`.

## Why one combined model

A Power BI report connects to exactly **one** semantic model, but these seven pages need measures
from all four domain models in `semantic/` (scorecard: Sales; claims: Claims; filing:
Compliance; P&L: Finance). So `model.py` builds one internal **reporting model**, `F&I Data Platform`,
from the same plan as `python -m semantic`: the same 80 measures (each still maps to one metric
in `dbt/models/metrics.yml`), the same Time Intelligence calculation group, relationships and RLS
rules, over every fact and conformed dimension. The only addition is `IBNR by Cohort`, the
actuarial valuation output (`gold_actuarial.ibnr_by_cohort`). It is not a dbt model, so its four
measures live on that table with a `source = actuarial` annotation, not in `Key Measures`. It has
no dealer or tenant key, so the slicers do not filter it.

Security: the model carries the Internal, Tenant Partner and Dealer User roles so you can test
with "View as". External roles get a `FALSE()` filter on the funded-reserve filing, the filed
lines and IBNR, because compliance data is internal only (`docs/ACCESS_MATRIX.md`). External
dealers keep using the Sales, Claims and Finance domain models.

The measure table is called **Key Measures**: Power BI Desktop refuses to open a model with a
table named `Measures` ("Unsupported Table name"), so the domain models were renamed too.

## Format choices

- **PBIR** (the folder-per-visual report format), schema versions: report 3.3.0, page 2.1.0,
  visualContainer 2.13.0. Power BI Desktop 2.159 (Microsoft Store, October 2026) opened it with
  **no preview features turned on**.
- **TMDL** for the model, compatibility level 1601, Import mode, PostgreSQL connector. The server
  and database are the model parameters `PostgresServer` (`localhost:5432`) and
  `PostgresDatabase` (`vsc_analytics`): change them in Transform data > Manage parameters.
- Theme: `FI Data Platform.Report/StaticResources/RegisteredResources/FI_Data_Platform_Theme.json`, the
  report site's light palette (same series colours, ink, page and border tokens).
- Layout grid: 1280 x 720 page, 24 px margin, 12 columns of 88 px with 16 px gutters. Rows are
  header, filters, KPI cards, then two chart rows. Every visual has a title and an "i" tooltip
  that explains what it shows.

## Rebuild and check

```powershell
.venv\Scripts\python -m powerbi                # regenerate the .pbip, report and model
.venv\Scripts\python -m powerbi --check        # exit 1 if the committed files are stale
.venv\Scripts\python -m powerbi --validate     # schemas + field references + layout
.venv\Scripts\python -m pytest tests\test_powerbi.py
```

`--validate` downloads Microsoft's schemas once (the `$schema` URL in each file, from
developer.microsoft.com; the theme schema from Microsoft's powerbi-desktop-samples repository) into
`data/powerbi_schemas/`. The live test in `tests/test_powerbi.py` checks every imported table and
column against the Postgres server and skips when the server is not running.

## Customize it yourself (to learn)

The generator owns the files in this folder, so `--check` reports any edit you save here. To
experiment freely, open the project and use **File > Save as** to save a copy under another name,
for example `Documents\My Portfolio.pbip`, outside the repo so it never gets committed. Then try these, roughly easiest first:

1. **Change a KPI card's window.** Select the "Written premium · T12M" card, open the Filters
   pane, and change *Time Calculation* from T12M to YTD. That is the calculation group: one
   measure, any window, no `Written Premium YTD` copy.
2. **Add a slicer.** Insert a Slicer, drag `Dealer > Program` onto it. Watch every visual react.
3. **Conditional formatting.** On the dealer table, right-click *Cancel rate* in the Values well
   > Conditional formatting > Background color > Rules: red when it is 0.25 or more.
4. **Write a measure.** On `Key Measures`, New measure:
   `Premium per Contract = DIVIDE([Written Premium], [Contracts Written])`, then put it on a card.
   (In this project new measures belong in `dbt/models/metrics.yml`; this is for practice.)
5. **Drill with the field parameter.** Build a matrix, put `Drill Level` on Rows and
   `Earned Premium` on Values, then use a slicer on `Drill Level` to switch between tenant,
   dealer, product and state.
6. **Change the theme.** View > Themes > Customize current theme: change the first data colour
   and see every chart follow.
7. **Profile a slow visual.** Optimize > Performance analyzer > Start recording > refresh visuals,
   then copy a visual's DAX query into DAX Studio.
8. **Read the files.** After a save, open a `visual.json` in VS Code and find the field you
   dragged in, or a measure in `Key Measures.tmdl`. That is what code review of a Power BI report
   looks like.
