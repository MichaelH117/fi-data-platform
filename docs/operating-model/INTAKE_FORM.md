# Request intake form

Use this form for any new report, metric, extract, dashboard change or data fix. Paste it into a
new issue (label `intake`). The Reporting Manager triages new requests within 2 business days
([SLA.md](SLA.md)). Triage gives each request an **owner**, a **due date** and a
**definition of done**. Without all three, the request is not accepted yet.

Ad-hoc asks are batched into a twice-weekly triage, so analysts keep long focus blocks. Only Sev 1
and Sev 2 incidents interrupt.

---

**If it is not on the ticket, it is not in scope.** Requests that arrive by email or chat are moved
onto a ticket before any work starts, and the original request email is attached to it. From then
on the ticket is the only source: a change agreed in a call or a chat is added to the ticket, or it
did not happen.

## 1. Requester
| Field | Answer |
|---|---|
| Name / team | |
| Requesting department | the department that owns the decision (not only the person asking) |
| Original request date | when the request first arrived, even if it was by email or chat |
| Audience of the output | executive / finance / claims / field team / dealer / partner program / regulator |
| Date needed by, and why | |
| Is this replacing an existing report or spreadsheet? Which one? | |

## 2. The question
**What decision will this help someone make?** One or two sentences. ("Which dealers should we
call this month about late remittance?" is a good answer. "A remittance report" is not.)

| Field | Answer |
|---|---|
| Change reason | why now: new regulation, a number that looks wrong, a new partner program, a retired spreadsheet, ... |
| Acceptance criteria | copied word for word from the requester; do not reword them. Triage may add criteria below, marked as added |
| Attachments | the original request email; for a regulatory filing, the regulator's blank form (PDF) |

## 3. What you need
- [ ] New report or page
- [ ] Change to an existing report (name it)
- [ ] New or changed metric (it will be defined once, in `docs/METRICS.md` and `dbt/models/metrics.yml`)
- [ ] Data extract for a dealer or partner (usually already covered by the dealer packets or the raw-data portal)
- [ ] Data correction (a number looks wrong; say where you saw it and what you expected)
- [ ] Access change (see `docs/ACCESS_MATRIX.md`)

## 4. Definitions (the part that causes most rework)
| Item | Answer |
|---|---|
| Grain: one row per... | contract / claim / dealer / month / ... |
| Statuses counted | in force only / written (pending + in force + cancelled + expired) / other |
| Scope | inception to date as of period end / within the period |
| Money basis | coverage cost / retail price / both |
| Period | calendar month / fiscal / a regulatory period (for example Sep-Aug) |
| Filters and exclusions | products, states, dealers, partner programs |
| Existing number it must tie to | for example, "the dealer production total" or "the GL premium" |

Counting rules are stored as **seeds** (for example `dbt/seeds/regulatory/report_definitions.csv`),
never as spreadsheet comments. If your request has one, it becomes a seed row with a test.

## 5. Sensitivity
- [ ] Contains customer PII (names, VINs, addresses): needs Director approval, and PII never leaves staging except on flagged marts
- [ ] Goes outside the company (dealer, partner, regulator)
- [ ] Shows one dealer's numbers to another party: needs peer suppression (publish layer)

---

## Triage (Reporting Manager fills in)
| Field | Value |
|---|---|
| Ticket | |
| Priority | P1 (incident) / P2 (this sprint) / P3 (backlog) / declined, with reason |
| Owner (role) | |
| Reviewer (role) | a peer, never the owner ([RELEASE.md](RELEASE.md) checklist) |
| Due date | |
| Story points | 1 / 2 / 3 / 5 / 8; anything above 8 is split before it enters a sprint |
| Reuses | existing marts and metrics it builds on |
| Definition of done | numbers tie to `<control total>`; filters documented on the page; RLS / suppression tested; added to `docs/REPORTS.md` |
| Usage review date | 90 days after launch: retire it if nobody opens it |
