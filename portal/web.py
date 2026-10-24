"""Browser UI for the raw-data portal (stdlib only), served by ``python -m portal serve``.

    GET  /ui/login             sign-in form (portal login + API key)
    POST /ui/login             check the key, start a session (HttpOnly, SameSite=Strict cookie)
    GET  /ui                   datasets, preview (first 100 rows), since filter, access-log panel
    GET  /ui/download/<name>   the same rows as a CSV or JSON attachment (``format=csv|json``)
    POST /ui/logout            end the session

The session stores only the portal user's resolved scope and role, never the API key. Every
request re-derives what may be read from that session on the server: a ``dealer`` parameter can
only narrow a tenant admin to one of its own dealers, and ``scope=contact`` is refused unless the
user is that dealer's DEALER_ADMIN. Each preview and download writes one access-log row
(channel ``ui``), and the log panel shows that dealer's own history only.
"""

from __future__ import annotations

import html
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pandas as pd

from portal import audit, query

COOKIE = "portal_session"
IDLE_SECONDS = 30 * 60
PREVIEW_ROWS = 100
STATIC = Path(__file__).with_name("static")
STATIC_FILES = {"portal.css": "text/css; charset=utf-8", "portal.js": "text/javascript; charset=utf-8"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Session:
    login: str
    role: str
    scope: query.Scope
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    expires: float = 0.0


class Sessions:
    """In-memory session store (local demo; sessions end when the server stops)."""

    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, login: str, role: str, scope: query.Scope) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = Session(login, role, scope, expires=time.time() + IDLE_SECONDS)
        return token

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._items.get(token)
            if s is None or s.expires < time.time():
                self._items.pop(token, None)
                return None
            s.expires = time.time() + IDLE_SECONDS
            return s

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._items.pop(token or "", None)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _cell(value: object) -> str:
    try:
        missing = bool(pd.isna(value))
    except (TypeError, ValueError):
        missing = False
    return "" if missing else _e(value)


def _valid_since(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValueError("the date filter must look like YYYY-MM-DD") from exc


def history(admin_core: Path, scope: query.Scope, login: str, limit: int = 25) -> list[dict]:
    """Access-log rows of this scope: successful reads of its data, plus this login's own refusals."""
    path = audit.log_path(admin_core)
    if not path.exists():
        return []
    sql = ("SELECT accessed_at, channel, login, dealer_number, data_scope, dataset, row_count, since, status "
           "FROM portal_access_log WHERE tenant_code = ? AND (status = 'OK' OR login = ?)")
    params: list = [scope.tenant_code, login]
    if scope.dealer_number is not None:
        sql += " AND dealer_number = ?"
        params.append(scope.dealer_number)
    sql += " ORDER BY access_id DESC LIMIT ?"
    params.append(limit)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.OperationalError:  # log file exists but has no table yet
            return []
    for r in rows:  # never show an operator's OS user name to a dealer
        if str(r["login"]).startswith("operator:"):
            r["login"] = "operator"
    return rows


class WebUI:
    def __init__(self, db_path: Path, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self.db_path, self.conn, self.lock = db_path, conn, lock
        self.sessions = Sessions()

    # ---------------------------------------------------------------- plumbing
    @staticmethod
    def _respond(h, status: HTTPStatus, body: bytes = b"", ctype: str = "text/html; charset=utf-8",
                 headers: dict[str, str] | None = None) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _redirect(self, h, location: str, headers: dict[str, str] | None = None) -> None:
        self._respond(h, HTTPStatus.SEE_OTHER, b"", headers={"Location": location, **(headers or {})})

    @staticmethod
    def _token(h) -> str | None:
        raw = h.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is just no session
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    @staticmethod
    def _form(h) -> dict[str, str]:
        n = min(int(h.headers.get("Content-Length") or 0), 8192)
        data = h.rfile.read(n).decode("utf-8", "replace") if n > 0 else ""
        return {k: v[-1] for k, v in parse_qs(data).items()}

    def _audit(self, s: Session, scope: query.Scope | None, *, dataset: str, data_scope: str, rows: int | None,
               since: str | None, status: str = "OK", message: str | None = None, dealer: str | None = None) -> None:
        audit.record(self.db_path, channel="ui", login=s.login, role=s.role,
                     tenant_code=s.scope.tenant_code,
                     dealer_number=scope.dealer_number if scope else (dealer or s.scope.dealer_number),
                     data_scope=(data_scope or "standard")[:20], dataset=dataset[:32], row_count=rows,
                     since=(since or None) and since[:32], status=status, message=message)

    # ---------------------------------------------------------------- routing
    def handles(self, path: str) -> bool:
        return path == "/" or path == "/ui" or path.startswith("/ui/")

    def dispatch(self, h, method: str) -> None:
        url = urlparse(h.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        if path == "/":
            return self._redirect(h, "/ui")
        if path.startswith("/ui/static/") and method == "GET":
            return self._static(h, path.rsplit("/", 1)[-1])
        if path == "/ui/login":
            return self._login_post(h) if method == "POST" else self._login_page(h)
        s = self.sessions.get(self._token(h))
        if path == "/ui/logout" and method == "POST":
            form = self._form(h)
            if s and secrets.compare_digest(form.get("csrf", ""), s.csrf):
                self.sessions.drop(self._token(h))
            return self._redirect(h, "/ui/login", {"Set-Cookie": f"{COOKIE}=; Path=/ui; Max-Age=0; HttpOnly; "
                                                                 "SameSite=Strict"})
        if s is None:
            return self._redirect(h, "/ui/login")
        if method != "GET":
            return self._respond(h, HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed", "text/plain")
        if path == "/ui":
            return self._home(h, s, params)
        parts = path.split("/")
        if len(parts) == 4 and parts[2] == "download" and parts[3] in query.DATASETS:
            return self._download(h, s, parts[3], params)
        return self._respond(h, HTTPStatus.NOT_FOUND, self._page("Not found", "<p>Page not found.</p>", s))

    def _static(self, h, name: str) -> None:
        if name not in STATIC_FILES:
            return self._respond(h, HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        body = (STATIC / name).read_bytes()
        self._respond(h, HTTPStatus.OK, body, STATIC_FILES[name], {"Cache-Control": "max-age=300"})

    # ---------------------------------------------------------------- sign-in
    def _login_page(self, h, error: str | None = None, login: str = "",
                    status: HTTPStatus = HTTPStatus.OK) -> None:
        err = f'<p class="alert alert-critical" role="alert">{_e(error)}</p>' if error else ""
        body = f"""
<section class="signin card">
  <h1>Sign in</h1>
  <p class="muted">Dealer raw-data portal. Use your portal login and API key.</p>
  {err}
  <form method="post" action="/ui/login" class="stack">
    <label>Login <input name="login" autocomplete="username" required value="{_e(login)}"
      placeholder="d1001.admin"></label>
    <label>API key <input name="api_key" type="password" autocomplete="current-password" required></label>
    <button type="submit" class="btn btn-primary">Sign in</button>
  </form>
  <details class="hint"><summary>Demo credentials</summary>
    <p>All data is synthetic. Demo logins look like <code>d1001.admin</code> (dealer admin),
    <code>d1003.viewer</code> (dealer viewer) or <code>pva.admin</code> (tenant admin). Print a login's
    demo key with <code>python -m portal demo-key --login d1001.admin</code>.</p>
  </details>
</section>"""
        self._respond(h, status, self._page("Sign in", body, None))

    def _login_post(self, h) -> None:
        form = self._form(h)
        login = form.get("login", "").strip()[:64]
        try:
            with self.lock:
                scope, role = query.authenticate(self.conn, login, form.get("api_key", ""))
        except query.AccessDenied:
            audit.record(self.db_path, channel="ui", login=login or "-", role=None, tenant_code=None,
                         dealer_number=None, data_scope="standard", dataset="sign-in", row_count=None,
                         status="DENIED", message="invalid credentials")
            return self._login_page(h, "Login or API key is not valid.", login, HTTPStatus.UNAUTHORIZED)
        token = self.sessions.create(login, role, scope)
        cookie = f"{COOKIE}={token}; Path=/ui; HttpOnly; SameSite=Strict; Max-Age={IDLE_SECONDS}"
        self._redirect(h, "/ui", {"Set-Cookie": cookie})

    # ---------------------------------------------------------------- data
    def _resolve(self, s: Session, params: dict[str, str]) -> tuple[query.Scope, str, str | None]:
        """Scope + data scope + since for this request, all checked against the session."""
        data_scope = params.get("scope") or "standard"
        since = _valid_since(params.get("since"))
        with self.lock:
            scope = query.narrow(self.conn, s.scope, params.get("dealer") or None)
            query.check_data_scope(s.role, scope, data_scope)
        return scope, data_scope, since

    def _denied(self, h, s: Session, dataset: str, params: dict[str, str], exc: Exception) -> None:
        self._audit(s, None, dataset=dataset, data_scope=params.get("scope", "standard"), rows=None,
                    since=params.get("since"), status="DENIED", message=str(exc)[:200],
                    dealer=(params.get("dealer") or "")[:16] or None)
        body = (f'<p class="alert alert-critical" role="alert">Access denied: {_e(exc)}.</p>'
                '<p><a href="/ui">Back to your data</a></p>')
        self._respond(h, HTTPStatus.FORBIDDEN, self._page("Access denied", body, s))

    def _fetch(self, scope: query.Scope, dataset: str, since: str | None, data_scope: str):
        with self.lock:
            return query.fetch(self.conn, scope, dataset, since, data_scope)

    def _download(self, h, s: Session, dataset: str, params: dict[str, str]) -> None:
        fmt = "json" if params.get("format") == "json" else "csv"
        try:
            scope, data_scope, since = self._resolve(s, params)
        except (query.AccessDenied, ValueError) as exc:
            return self._denied(h, s, dataset, params, exc)
        df = self._fetch(scope, dataset, since, data_scope)
        self._audit(s, scope, dataset=dataset, data_scope=data_scope, rows=len(df), since=since,
                    message=f"download {fmt}")
        name = f"{scope.tenant_code}_{scope.dealer_number or 'all'}_{dataset}.{fmt}"
        if fmt == "csv":
            body, ctype = df.to_csv(index=False, lineterminator="\n").encode(), "text/csv; charset=utf-8"
        else:
            body, ctype = df.to_json(orient="records", date_format="iso", indent=1).encode(), "application/json"
        self._respond(h, HTTPStatus.OK, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})

    def _dealer_options(self, s: Session) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT dealer_number FROM dealer WHERE tenant_id = ? AND removed_from_source = 0 "
                "ORDER BY dealer_number", [s.scope.tenant_id])]

    def _home(self, h, s: Session, params: dict[str, str]) -> None:
        dataset = params.get("dataset", "contracts")
        if dataset not in query.DATASETS:
            dataset = "contracts"
        try:
            scope, data_scope, since = self._resolve(s, params)
        except (query.AccessDenied, ValueError) as exc:
            return self._denied(h, s, dataset, params, exc)
        df = self._fetch(scope, dataset, since, data_scope)
        shown = df.head(PREVIEW_ROWS)
        self._audit(s, scope, dataset=dataset, data_scope=data_scope, rows=len(shown), since=since,
                    message=f"preview of {len(df)} rows")
        keep = {k: v for k, v in {"since": since, "scope": data_scope if data_scope == "contact" else None,
                                  "dealer": scope.dealer_number if s.scope.dealer_id is None else None}.items() if v}
        body = "\n".join([
            self._header(s, scope),
            self._tabs(dataset, keep),
            self._filters(s, scope, dataset, data_scope, since),
            self._preview(dataset, df, shown, data_scope, keep),
            self._log_panel(s, scope),
        ])
        self._respond(h, HTTPStatus.OK, self._page(f"{dataset.title()} - raw data", body, s))

    # ---------------------------------------------------------------- views
    @staticmethod
    def _header(s: Session, scope: query.Scope) -> str:
        who = f"dealer {scope.dealer_number}" if scope.dealer_number else "all dealers"
        return f"""
<header class="page-head">
  <h1>Your raw data</h1>
  <p class="muted">Tenant <strong>{_e(scope.tenant_code)}</strong> &middot; {_e(who)} &middot; signed in as
  <code>{_e(s.login)}</code> <span class="pill">{_e(s.role)}</span></p>
</header>"""

    @staticmethod
    def _tabs(current: str, keep: dict[str, str]) -> str:
        links = []
        for name in query.DATASETS:
            q = urlencode({"dataset": name, **keep})
            cur = ' aria-current="page"' if name == current else ""
            links.append(f'<a href="/ui?{_e(q)}"{cur}>{_e(name.title())}</a>')
        return f'<nav class="tabs" aria-label="Datasets">{"".join(links)}</nav>'

    def _filters(self, s: Session, scope: query.Scope, dataset: str, data_scope: str, since: str | None) -> str:
        dealer = ""
        if s.scope.dealer_id is None:  # tenant admin: may narrow to one of its own dealers
            opts = ['<option value="">All dealers</option>'] + [
                f'<option value="{_e(d)}"{" selected" if d == scope.dealer_number else ""}>{_e(d)}</option>'
                for d in self._dealer_options(s)]
            dealer = f'<label>Dealer <select name="dealer">{"".join(opts)}</select></label>'
        contact = ""
        if s.role in query.CONTACT_ROLES and s.scope.dealer_id is not None:
            checked = " checked" if data_scope == "contact" else ""
            contact = (f'<label class="check"><input type="checkbox" name="scope" value="contact"{checked}> '
                       'Include customer contact data <span class="muted">(dealer admin; logged)</span></label>')
        return f"""
<form class="filters card" method="get" action="/ui">
  <input type="hidden" name="dataset" value="{_e(dataset)}">
  <label>Updated since <input type="date" name="since" value="{_e(since or '')}"></label>
  {dealer}{contact}
  <button type="submit" class="btn btn-primary">Apply</button>
  <a class="btn" href="/ui?dataset={_e(dataset)}">Reset</a>
</form>"""

    @staticmethod
    def _preview(dataset: str, df, shown, data_scope: str, keep: dict[str, str]) -> str:
        q = urlencode(keep)
        sep = "&" if q else ""
        csv = f"/ui/download/{dataset}?{q}{sep}format=csv"
        js = f"/ui/download/{dataset}?{q}{sep}format=json"
        if data_scope == "contact":
            note = ('<p class="alert alert-warning">Contact scope: customer names, e-mail, phone, ZIP and full VINs '
                    'are shown. This read is in the access log.</p>')
        else:
            note = ('<p class="alert alert-info">Customer data is masked: names as initials, VINs to the last 6 '
                    'characters, no e-mail, phone or ZIP.</p>')
        if shown.empty:
            table = '<p class="empty">No rows match this filter.</p>'
        else:
            head = "".join(f"<th scope=\"col\">{_e(c)}</th>" for c in shown.columns)
            rows = "".join("<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in rec) + "</tr>"
                           for rec in shown.itertuples(index=False, name=None))
            table = (f'<div class="table-wrap" tabindex="0" role="region" aria-label="{_e(dataset)} preview">'
                     f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>")
        return f"""
<section class="card">
  <div class="card-head">
    <h2>{_e(dataset.title())} <span class="muted">showing {len(shown):,} of {len(df):,} rows</span></h2>
    <div class="actions"><a class="btn" href="{_e(csv)}">Download CSV</a>
      <a class="btn" href="{_e(js)}">Download JSON</a></div>
  </div>
  {note}
  {table}
</section>"""

    def _log_panel(self, s: Session, scope: query.Scope) -> str:
        rows = history(self.db_path, scope, s.login)
        if not rows:
            inner = '<p class="empty">No access recorded yet.</p>'
        else:
            body = "".join(
                f"<tr><td>{_e(r['accessed_at'])}</td><td>{_e(r['channel'])}</td><td><code>{_e(r['login'])}</code></td>"
                f"<td>{_e(r['dealer_number'] or '-')}</td><td>{_e(r['dataset'])}</td><td>{_e(r['data_scope'])}</td>"
                f"<td class=\"num\">{_e('' if r['row_count'] is None else r['row_count'])}</td>"
                f"<td>{_e(r['since'] or '')}</td>"
                f"<td><span class=\"status status-{_e(str(r['status']).lower())}\">{_e(r['status'])}</span></td></tr>"
                for r in rows)
            inner = ('<div class="table-wrap" tabindex="0" role="region" aria-label="Access log"><table><thead><tr>'
                     "<th>When</th><th>Channel</th><th>Login</th><th>Dealer</th><th>Dataset</th><th>Scope</th>"
                     '<th class="num">Rows</th><th>Since</th><th>Status</th></tr></thead>'
                     f"<tbody>{body}</tbody></table></div>")
        return f"""
<section class="card">
  <div class="card-head"><h2>Access log <span class="muted">latest 25 reads of this data</span></h2></div>
  {inner}
</section>"""

    @staticmethod
    def _page(title: str, body: str, s: Session | None) -> bytes:
        out = ""
        if s is not None:
            out = (f'<form method="post" action="/ui/logout" class="signout"><input type="hidden" name="csrf" '
                   f'value="{_e(s.csrf)}"><button type="submit" class="btn">Sign out</button></form>')
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} | F&amp;I Data Platform raw-data portal</title>
<link rel="stylesheet" href="/ui/static/portal.css">
<script src="/ui/static/portal.js" defer></script>
</head>
<body>
<header class="topbar"><div class="topbar-inner">
  <a class="brand" href="/ui"><span class="brand-mark" aria-hidden="true"></span>F&amp;I Data Platform
    <span class="brand-sub">raw-data portal</span></a>
  <span class="spacer"></span>
  <button type="button" class="theme-toggle" id="theme-toggle" aria-label="Toggle dark mode">&#9680;</button>
  {out}
</div></header>
<main>
{body}
<p class="foot muted">Synthetic demo data. Read-only; every read is logged.</p>
</main>
</body>
</html>""".encode()
