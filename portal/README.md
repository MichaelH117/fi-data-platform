# Raw-data portal

A dealer pulls its own raw contract data out of Admin Core: the CLI export, a small read-only HTTP API and a
browser UI, all served from the standard library. All data is synthetic.

```powershell
.venv\Scripts\python.exe -m portal export --tenant PVA --dealer D1001      # files for one dealer
.venv\Scripts\python.exe -m portal serve                                  # API + UI on http://127.0.0.1:8765
.venv\Scripts\python.exe -m portal demo-key --login d1001.admin           # synthetic demo API key of a login
```

## Browser UI

Open <http://127.0.0.1:8765/ui> after `serve` and sign in with a portal login and its demo API key
(`demo-key` above). Demo logins: `d1001.admin` (dealer admin), `d1003.viewer` (dealer viewer), `pva.admin`
(tenant admin).

| Part | What it does |
|---|---|
| Datasets | Tabs for contracts, claims, cancellations and remittances of the signed-in dealer |
| Preview | First 100 rows, with the total row count |
| Updated since | Date filter (`YYYY-MM-DD`) on the contract's `updated_at` |
| Download | CSV or JSON of every matching row (not just the preview) |
| Contact data | A checkbox shown only to a `DEALER_ADMIN`; adds customer name, e-mail, phone, ZIP and full VIN |
| Access log | The latest 25 reads of this dealer's data, plus the user's own refused requests |

A tenant admin also gets a dealer picker limited to its own tenant's dealers, and never the contact scope.

## Security model

- **Scope from the session, not the request.** Sign-in checks the API key (SHA-256 hash, constant-time compare)
  and stores only the resolved tenant, dealer and role in an in-memory session. Every page and download
  re-checks on the server: `dealer=` can only narrow a tenant admin to its own dealers, and `scope=contact`
  is refused (403, logged) unless the user is that dealer's `DEALER_ADMIN`. The URL cannot widen access.
- **PII masked by default.** The standard scope shows initials and VINs masked to the last 6 characters, with no
  e-mail, phone or ZIP (`portal/query.py`). The UI uses the same queries as the CLI and API.
- **Every read is logged.** Each preview, download, refusal and failed sign-in writes one row to
  `portal_access_log` with channel `ui` (`portal/audit.py`). The log never stores the data or the key.
- **Cookie and headers.** The session cookie is `HttpOnly; SameSite=Strict; Path=/ui`, idle timeout 30 minutes;
  sign-out needs a per-session CSRF token. Pages send a strict Content-Security-Policy (no inline script),
  `nosniff`, `no-referrer` and `no-store`. All values are HTML-escaped.
- **Local demo only.** Sessions live in memory and the server speaks plain HTTP on `127.0.0.1`. Put it behind
  TLS and a real identity provider before any non-local use.

## Files

| File | Role |
|---|---|
| `query.py` | Scope resolution, authentication, data minimisation, dataset SQL |
| `audit.py` | Access-log writer |
| `server.py` | HTTP server: `/health`, `/api/v1/<dataset>`, and the UI routes |
| `web.py` | UI: sessions, sign-in, pages, downloads, access-log panel |
| `static/` | `portal.css` (report-site tokens, light + dark) and `portal.js` (theme toggle) |

Tests: `tests/test_portal.py` (cross-dealer and cross-tenant access denied, PII masked by default, contact scope
for the dealer admin only, an audit row per read, own-history-only log panel, session and CSRF checks).
