# Incident report template

Copy this file to `postmortems/YYYY-MM-<short-name>.md` and open it while the incident is live.
Fill in the timeline as you go and the rest within 5 business days. The review is **blameless**: it
describes what the system let happen, not who slipped. Every action item gets an owner (a role) and
a due date, and goes into the intake queue ([INTAKE_FORM.md](INTAKE_FORM.md)).

---

## <Title: what broke, in plain words>

| Field | Value |
|---|---|
| Severity | Sev 1 / 2 / 3 ([SLA.md](SLA.md)) |
| Status | Investigating / Mitigated / Resolved / Postmortem complete |
| Detected | YYYY-MM-DD HH:MM, by (gate name / stakeholder / analyst review) |
| Resolved | YYYY-MM-DD HH:MM |
| Incident lead | role |
| Affected run ids | `2026...Z` |
| Affected outputs | reports, pages, dealer packets, filings, extracts |
| Audience reached | none (caught by a gate) / internal / external |
| SLA impact | met / missed on N business days |

### Summary
Two or three sentences: what happened, who was affected, how it was fixed.

### Impact
- Which numbers were wrong or late, by how much (rows, dollars, percentage points).
- Which audiences saw them and for how long.
- Whether anything must be restated or re-filed.

### Timeline (local time)
| Time | Event |
|---|---|
| HH:MM | First signal (alert, gate failure, question from a stakeholder) |
| HH:MM | Incident opened, severity set |
| HH:MM | Stakeholder notice sent |
| HH:MM | Cause found |
| HH:MM | Fix merged, re-run started from `<step>` |
| HH:MM | Gates pass, reports published |

### Detection
How we found out. If a stakeholder found it before a gate did, that is the most important finding of
this review.

### Root cause
The chain of causes, in "because" steps. Stop at something the team can change (a process, a check,
a contract), not at a person.

### What went well / what went poorly
- Went well:
- Went poorly:
- Where we got lucky:

### Action items
| # | Action | Type (prevent / detect / mitigate / document) | Owner (role) | Due | Ticket |
|---|---|---|---|---|---|
| 1 | | | | | |

### Verification
How we will know the fix works: the test, gate or reconciliation check that would now fail, and the
run id where it was proven (for example, an injected failure in `tests/`).
