# Access matrix (Power BI style)

This file sets who sees what, where, and through which security layer. It covers the four
generated semantic models in `semantic/model/` (Sales, Claims, Finance, Compliance) and the reports
built on them.

**Rule:** people get access through **security groups**, never one by one. Workspace roles control
who can *build*. Apps and RLS roles control who can *read*. Every name below is a placeholder:
create the groups in your directory under these names.

## 1. Workspaces and apps

| Workspace | Stage | Contents | Published as app | Data source binding |
|---|---|---|---|---|
| `VSC Analytics [Dev]` | Development | All four models plus draft reports | no | `AnalyticsDatabase = ANALYTICS_DEV` (a transient clone) |
| `VSC Analytics [Test]` | UAT | Same items, promoted by the deployment pipeline | no | `ANALYTICS` zero-copy UAT clone (`warehouse/snowflake/07_*`) |
| `VSC Analytics` | Production | Certified models plus internal reports | **VSC Internal** app | `ANALYTICS` |
| `VSC Compliance` | Production | Compliance model, state filings, market survey | **VSC Compliance** app | `ANALYTICS` |
| `VSC Partner Portal` | Production | Thin reports on Sales, Claims and Finance with RLS, plus tenant branding | **Dealer & Partner Reports** app | `ANALYTICS` |

How items move from Dev to Prod:

- Items are promoted only by the **deployment pipeline**. Nobody publishes straight to Test or Prod.
- Deployment rules switch the `AnalyticsDatabase` parameter for each stage.
- The TMDL in `semantic/model/` is the source. Release = `python -m semantic --check` passes, then the
  deployment pipeline promotes Dev to Test, then Test to Prod (see
  `docs/operating-model/RELEASE.md`).
- The models are **certified** in Prod. Reports connect live to them, so nobody imports a private
  copy.

## 2. Audiences

| Audience | Who | Gets | RLS role | Models |
|---|---|---|---|---|
| Executive | Leadership | VSC Internal app | Internal | Sales, Claims, Finance |
| Finance | Financial analysts, controller | VSC Internal app, Build on Finance | Internal | Finance, Sales |
| Claims operations | Claims managers, adjusters' leads | VSC Internal app | Internal | Claims |
| Compliance | Regulatory analysts | VSC Compliance app, Build on Compliance | Internal | Compliance |
| Field team | Account managers for dealers | VSC Internal app | Internal | Sales, Claims |
| Data team | Analysts, BI developers, engineers | Workspace roles (section 3) | Internal (test the other roles with "View as") | all |
| External partner | White-label partner program staff | Dealer & Partner Reports app | Tenant Partner | Sales, Claims, Finance |
| Dealer | Dealer principals, F&I and office staff | Dealer & Partner Reports app, plus the per-dealer packets | Dealer User | Sales, Claims, Finance |

External audiences (partners and dealers):

- They never get a workspace role, Build permission, export of underlying data, or the Compliance
  model.
- They sign in as guest accounts in the external groups.

## 3. Workspace roles

| Role | Can do | Dev | Test | Prod (`VSC Analytics`, `VSC Compliance`) | Prod (`VSC Partner Portal`) |
|---|---|---|---|---|---|
| Admin | Manage access, settings, pipeline | `sg-bi-admins` | `sg-bi-admins` | `sg-bi-admins` | `sg-bi-admins` |
| Member | Publish, share, update the app | `sg-bi-developers` | `sg-bi-developers` | `sg-bi-release-managers` | `sg-bi-release-managers` |
| Contributor | Publish content, no sharing | `sg-bi-analysts` | `sg-bi-analysts` | none | none |
| Viewer | Read in the workspace (RLS applies) | `sg-bi-testers` | `sg-bi-uat-business` | none (people use the app) | none |

Admins, Members and Contributors **bypass RLS**. That's why no external user, and no
business-only user, ever holds one of those roles in a workspace with external data.

## 4. RLS roles (in every generated model)

| RLS role | Filter (dynamic, from `Security User Access`) | Mapped security groups | Models |
|---|---|---|---|
| Internal | No row filter | `sg-bi-internal-sales`, `sg-bi-internal-claims`, `sg-bi-internal-finance`, `sg-bi-internal-compliance` | all four |
| Tenant Partner | `Tenant` and every tenant-keyed dimension limited to the signed-in user's tenants; the split contract dims (`Contract Terms`, `Contract Finance`, `Contract Vehicle`, `Contract Actuarial Class`, `Contract Claim Summary`) limited to the same tenants' contracts | `sg-bi-partner-<tenant>` (one per white-label tenant) | Sales, Claims, Finance |
| Dealer User | Tenant as above, plus `Dealer`, `Dealer Group`, `Contract`, `Claim`, `Participation Entity` and the split contract dims limited to the user's dealers; `Account Holder` limited to payees paid on the user's dealers' business (hidden `Payee Dealer Access` bridge) | `sg-bi-dealer-users` | Sales, Claims, Finance |

How the dynamic map works:

- `Security User Access` is built from `core.dim_user_dealer_access`, one row per user per dealer.
- It is hidden, has no relationships, and each user can read only their own rows.
- The split contract dims hang off the facts, not off `Contract`, so `Contract`'s filter never reaches
  them. Each role repeats the contract rule on them (`[Contract Key] IN` the allowed contracts), and
  `tests/test_semantic.py` fails if any table with a contract, dealer or tenant key is left unfiltered.
- The same map drives the per-dealer publish packets (`publish/packets.py`) and the raw-data
  portal (`portal/`). One user can't see different scopes in different places.
- In production the `user_login` column holds the Entra ID user principal name.
- Each role annotation in the TMDL names its security group.

## 5. Security groups

| Group | Members | Grants |
|---|---|---|
| `sg-bi-admins` | Two platform owners (BI lead and DE manager) | Workspace Admin everywhere, deployment pipeline owner |
| `sg-bi-release-managers` | BI lead, reporting manager | Member in Prod. Updates the apps |
| `sg-bi-developers` | BI developers, analytics engineers | Member in Dev and Test |
| `sg-bi-analysts` | Data and financial analysts | Contributor in Dev. Build on certified models |
| `sg-bi-testers` / `sg-bi-uat-business` | Data team / business UAT sign-off | Viewer in Dev / Test |
| `sg-bi-internal-<domain>` | Employees, by department | App audience plus Internal RLS role in that domain's model |
| `sg-bi-partner-<tenant>` | Partner program guest accounts | Partner app audience plus Tenant Partner role |
| `sg-bi-dealer-users` | Dealer guest accounts, provisioned from `portal_user` | Partner app audience plus Dealer User role |

## 6. Dev vs prod

| Concern | Dev | Prod |
|---|---|---|
| Warehouse database | `ANALYTICS_DEV` (transient) | `ANALYTICS` |
| Snowflake role used by the model | `REPORTER`, read-only on marts (`warehouse/snowflake/03_*`) | `REPORTER` |
| Snowflake warehouse | `REPORTING` (X-Small, auto-suspend) | `REPORTING` (X-Small, auto-suspend, resource monitor at 80% / 100%) |
| Refresh | On demand | Daily after the pipeline's gates pass (by 7 AM), with incremental refresh on large facts |
| Who can edit | `sg-bi-developers`, `sg-bi-analysts` | Nobody by hand. Deployment pipeline only |
| RLS testing | "View as" each role with test logins from `core.dim_user_dealer_access` | Quarterly access review of group membership |
| Data | Synthetic or cloned | Production marts (synthetic in this demo) |

## 7. Controls checklist (every release)

- [ ] `python -m semantic --check` passes. `tests/test_semantic.py` is green: every measure maps
  to a metric, there are no time-suffixed copies, and each role filter reaches every fact.
- [ ] There are no new external members in any workspace role.
- [ ] A new tenant has its own `sg-bi-partner-<tenant>` group, and its users are mapped in `portal_user`.
- [ ] Export of underlying data is off for the Partner Portal workspace.
- [ ] The Compliance app audience includes only `sg-bi-internal-compliance` and leadership.
- [ ] `python -m semantic access-audit` exits 0, and no item is shared directly (section 8).

## 8. App audiences and what each one sees

An app can carry several **audiences**. Each audience is one or more security groups (section 5) and
sees only the reports ticked for it; the rest of the app is hidden from it. Rules:

- **Audiences sit on top of RLS, never instead of it.** Hiding a report only tidies the navigation.
  The rows anyone reads still come from their RLS role (section 4), so a dealer who finds a hidden
  report's link sees their own dealers or nothing.
- **Shared directly: never.** No report, model or dashboard is shared by link or direct access. Every
  reader comes in through an app audience, so removing someone from a group removes all their access.
- External audiences live only in the Dealer & Partner Reports app. A report is visible to an audience
  only when every model it reads has that audience's RLS role (`spec.DOMAIN_ROLES`), so Compliance
  reports can never reach an external reader.
- Each row below is one thin report on the certified models. The generated report in `powerbi/`
  carries the internal ones as pages so authors can build and test them in one file.

`-` = the audience is not in that app.

| App | Report | Models | Executive | Finance | Claims operations | Compliance | Field team | External partner | Dealer | Shared directly |
|---|---|---|---|---|---|---|---|---|---|---|
| VSC Internal | Executive overview | Sales, Claims, Finance | visible | visible | visible | - | visible | - | - | never |
| VSC Internal | Dealer scorecard | Sales, Claims | visible | visible | visible | - | visible | - | - | never |
| VSC Internal | Claims & IBNR (IBNR is actuarial output) | Claims | visible | visible | visible | - | hidden | - | - | never |
| VSC Internal | Finance | Finance | visible | visible | hidden | - | hidden | - | - | never |
| VSC Internal | Experience (V6 edit 1) | Finance, Sales, Claims | visible | visible | visible | - | hidden | - | - | never |
| VSC Internal | New Reserves (V6 edit 11) | Finance, Sales | visible | visible | hidden | - | hidden | - | - | never |
| VSC Internal | Commission hierarchy (V6 edit 12; agent pay is compensation data) | Finance | visible | visible | hidden | - | hidden | - | - | never |
| VSC Compliance | Regulatory funded reserve | Compliance | visible | - | - | visible | - | - | - | never |
| VSC Compliance | State in-force contract lists (contract-level rows) | Compliance | hidden | - | - | visible | - | - | - | never |
| Dealer & Partner Reports | Dealer scorecard (partner view) | Sales, Claims | - | - | - | - | - | visible | visible | never |
| Dealer & Partner Reports | Claims (partner view) | Claims | - | - | - | - | - | visible | visible | never |
| Dealer & Partner Reports | Partner settlement statement (V6 edit 17; one statement per partner) | Finance | - | - | - | - | - | visible | hidden | never |

How it was (legacy): the app audience was the only boundary. Reports were hidden per audience, but
the models had no row filter, so anyone given a direct link or Build permission saw every dealer. Here
audiences sit on top of RLS, and nothing is shared directly.

Audit: `python -m semantic access-audit --out access_audit.csv` writes one row per app x audience x
report (visibility, RLS role, models, shared directly) and exits 1 when a rule above is broken.
`semantic/access.py` holds the list; `tests/test_semantic.py` fails if this table drifts from it.
