"""Raw-data portal: a dealer sees only its own tenant + dealer rows (CLI export and HTTP API)."""

from __future__ import annotations

import http.client
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import pytest

from generator import admin_core as ac
from generator.build import build, demo_api_key
from portal import query
from portal.__main__ import main as portal_main
from portal.server import serve

SEED = 9


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("portal")
    build(root / "admin_core.db", root / "legacy_admin.db", root / "landing", seed=SEED, as_of=date(2026, 10, 21),
          n_legacy=500, n_core=2500)
    return root / "admin_core.db"


def _dealers(db: Path) -> pd.DataFrame:
    with sqlite3.connect(db) as conn:
        return pd.read_sql("select d.dealer_id, d.dealer_number, d.tenant_id, a.administrator_code, "
                           "(select count(*) from contract c where c.dealer_id = d.dealer_id) n "
                           "from dealer d join administrator a using (tenant_id) where d.is_demo = 0 "
                           "order by n desc", conn)


def test_export_contains_only_the_dealers_rows(db: Path, tmp_path: Path) -> None:
    d = _dealers(db).iloc[0]
    assert portal_main(["--admin-core", str(db), "export", "--tenant", d["administrator_code"], "--dealer",
                        d["dealer_number"], "--out", str(tmp_path)]) == 0
    contracts = pd.read_csv(tmp_path / "contracts.csv")
    with sqlite3.connect(db) as conn:
        expected = conn.execute("select count(*) from contract where dealer_id = ? and removed_from_source = 0",
                                [int(d["dealer_id"])]).fetchone()[0]
    assert len(contracts) == expected > 0
    assert set(contracts["dealer_number"]) == {d["dealer_number"]}
    assert set(contracts["tenant_id"]) == {d["tenant_id"]}
    assert "row_version" not in contracts and "removed_from_source" not in contracts
    remits = pd.read_csv(tmp_path / "remittances.csv")
    assert set(remits["contract_number"]) <= set(contracts["contract_number"])
    meta = json.loads((tmp_path / "_export.json").read_text())
    assert meta["rows"]["contracts"] == expected


def test_export_refuses_a_dealer_from_another_tenant(db: Path, tmp_path: Path, capsys) -> None:
    dealers = _dealers(db)
    d = dealers.iloc[0]
    other = dealers[dealers["tenant_id"] != d["tenant_id"]]["administrator_code"].iloc[0]
    assert portal_main(["--admin-core", str(db), "export", "--tenant", other, "--dealer", d["dealer_number"],
                        "--out", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()
    assert "unknown tenant or dealer" in capsys.readouterr().err


def test_soft_deleted_rows_are_hidden(db: Path, tmp_path: Path) -> None:
    copy = tmp_path / "ac.db"
    copy.write_bytes(db.read_bytes())
    d = _dealers(copy).iloc[0]
    with sqlite3.connect(copy) as conn:
        cid = conn.execute("select contract_id from contract where dealer_id = ? limit 1",
                           [int(d["dealer_id"])]).fetchone()[0]
        ac.soft_delete(conn, "contract", [cid], "2026-10-22 09:00:00")
    with query.connect(copy) as conn:
        scope = query.resolve(conn, d["administrator_code"], d["dealer_number"])
        assert cid not in set(query.fetch(conn, scope, "contracts")["contract_id"])


def test_authentication_and_scope_narrowing(db: Path) -> None:
    dealers = _dealers(db)
    d = dealers.iloc[0]
    other_same_tenant = dealers[(dealers["tenant_id"] == d["tenant_id"]) & (dealers["dealer_id"] != d["dealer_id"])]
    login = f"{d['dealer_number'].lower()}.admin"
    with query.connect(db) as conn:
        with pytest.raises(query.AccessDenied):
            query.authenticate(conn, login, "wrong-key")
        scope, role = query.authenticate(conn, login, demo_api_key(SEED, login))
        assert role == "DEALER_ADMIN" and scope.dealer_number == d["dealer_number"]
        with pytest.raises(query.AccessDenied):
            query.narrow(conn, scope, other_same_tenant["dealer_number"].iloc[0])
        admin_login = f"{d['administrator_code'].lower()}.admin"
        tscope, _ = query.authenticate(conn, admin_login, demo_api_key(SEED, admin_login))
        assert tscope.dealer_id is None
        rows = query.fetch(conn, tscope, "contracts")
        assert set(rows["tenant_id"]) == {d["tenant_id"]}
        assert query.narrow(conn, tscope, other_same_tenant["dealer_number"].iloc[0]).dealer_id is not None
        foreign = dealers[dealers["tenant_id"] != d["tenant_id"]]["dealer_number"].iloc[0]
        with pytest.raises(query.AccessDenied):
            query.narrow(conn, tscope, foreign)


def test_http_api_enforces_the_portal_user_scope(db: Path) -> None:
    d = _dealers(db).iloc[0]
    login = f"{d['dealer_number'].lower()}.admin"
    httpd = serve(db, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path: str, user: str | None = None, key: str | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if user:
            req.add_header("X-Portal-User", user)
            req.add_header("X-Api-Key", key or "")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert get("/health")[0] == 200
        assert get("/api/v1/contracts")[0] == 401
        status, body = get("/api/v1/contracts", login, demo_api_key(SEED, login))
        assert status == 200 and body["rows"] > 0
        assert {r["dealer_id"] for r in body["data"]} == {int(d["dealer_id"])}
        other = _dealers(db).iloc[1]["dealer_number"]
        assert get(f"/api/v1/contracts?dealer={other}", login, demo_api_key(SEED, login))[0] == 403
        assert get("/api/v1/nope", login, demo_api_key(SEED, login))[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def _log(db: Path) -> pd.DataFrame:
    from portal.audit import log_path

    with sqlite3.connect(log_path(db)) as conn:
        return pd.read_sql("select * from portal_access_log order by access_id", conn)


def test_standard_export_has_no_customer_contact_data(db: Path, tmp_path: Path) -> None:
    d = _dealers(db).iloc[0]
    assert portal_main(["--admin-core", str(db), "export", "--tenant", d["administrator_code"], "--dealer",
                        d["dealer_number"], "--out", str(tmp_path)]) == 0
    contracts = pd.read_csv(tmp_path / "contracts.csv", dtype=str)
    assert not [c for c in contracts.columns if c.startswith("customer_")]
    assert not {"vin", "chassis_vin", "insurer_id", "lienholder_id", "original_obligor_id",
                "current_obligor_id"} & set(contracts.columns)
    assert contracts["vin_masked"].str.fullmatch(r"\*{11}[A-Z0-9]{6}").all()
    assert contracts["holder_initials"].dropna().str.fullmatch(r"[A-Z]\. [A-Z]\.").all()
    with sqlite3.connect(db) as conn:
        emails = {r[0] for r in conn.execute("select customer_email from contract where dealer_id = ?",
                                             [int(d["dealer_id"])]) if r[0]}
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.csv"))
    assert emails and not any(e in text for e in emails)
    log = _log(db)
    mine = log[(log["dealer_number"] == d["dealer_number"]) & (log["channel"] == "cli") & (log["status"] == "OK")]
    assert set(mine["dataset"]) == set(query.DATASETS) and set(mine["data_scope"]) == {"standard"}
    assert int(mine[mine["dataset"] == "contracts"]["row_count"].iloc[-1]) == len(contracts)
    assert mine["login"].str.startswith("operator:").all()


def test_contact_scope_is_for_the_dealer_admin_only(db: Path, tmp_path: Path) -> None:
    dealers = _dealers(db)
    d = dealers.iloc[0]
    args = ["--admin-core", str(db), "export", "--tenant", d["administrator_code"], "--dealer", d["dealer_number"],
            "--scope", "contact"]
    assert portal_main([*args, "--out", str(tmp_path / "anon")]) == 2  # no login: refused
    viewer = dealers[dealers["dealer_id"] % 3 == 0].iloc[0]  # these dealers also have a DEALER_VIEWER login
    vlogin = f"{viewer['dealer_number'].lower()}.viewer"
    assert portal_main(["--admin-core", str(db), "export", "--tenant", viewer["administrator_code"], "--dealer",
                        viewer["dealer_number"], "--scope", "contact", "--login", vlogin, "--api-key",
                        demo_api_key(SEED, vlogin), "--out", str(tmp_path / "viewer")]) == 2
    login = f"{d['dealer_number'].lower()}.admin"
    assert portal_main([*args, "--login", login, "--api-key", demo_api_key(SEED, login),
                        "--out", str(tmp_path / "admin")]) == 0
    full = pd.read_csv(tmp_path / "admin" / "contracts.csv", dtype=str)
    assert {"customer_email", "customer_phone", "vin"} <= set(full.columns)
    other = dealers[(dealers["tenant_id"] == d["tenant_id"]) & (dealers["dealer_id"] != d["dealer_id"])].iloc[0]
    assert portal_main(["--admin-core", str(db), "export", "--tenant", d["administrator_code"], "--dealer",
                        other["dealer_number"], "--scope", "contact", "--login", login, "--api-key",
                        demo_api_key(SEED, login), "--out", str(tmp_path / "foreign")]) == 2
    log = _log(db)
    assert (log["status"] == "DENIED").sum() >= 3
    ok = log[(log["login"] == login) & (log["data_scope"] == "contact") & (log["status"] == "OK")]
    assert ok["role"].eq("DEALER_ADMIN").all() and len(ok) == len(query.DATASETS)
    with query.connect(db) as conn:
        tscope, role = query.authenticate(conn, f"{d['administrator_code'].lower()}.admin",
                                          demo_api_key(SEED, f"{d['administrator_code'].lower()}.admin"))
        with pytest.raises(query.AccessDenied):
            query.check_data_scope(role, tscope, "contact")


def test_http_api_masks_by_default_and_logs_each_read(db: Path) -> None:
    d = _dealers(db).iloc[0]
    login = f"{d['dealer_number'].lower()}.admin"
    httpd = serve(db, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path: str, user: str, key: str):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        req.add_header("X-Portal-User", user)
        req.add_header("X-Api-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        before = len(_log(db)) if (db.with_name("portal_access_log.db")).exists() else 0
        status, body = get("/api/v1/contracts", login, demo_api_key(SEED, login))
        assert status == 200 and body["scope"] == "standard"
        assert not [k for k in body["data"][0] if k.startswith("customer_")]
        status, body = get("/api/v1/contracts?scope=contact", login, demo_api_key(SEED, login))
        assert status == 200 and "customer_email" in body["data"][0]
        admin = f"{d['administrator_code'].lower()}.admin"
        assert get("/api/v1/contracts?scope=contact", admin, demo_api_key(SEED, admin))[0] == 403
        log = _log(db).iloc[before:]
        assert list(log["status"]) == ["OK", "OK", "DENIED"] and set(log["channel"]) == {"api"}
        assert int(log["row_count"].iloc[0]) == body["rows"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- browser UI (/ui)
class _UI:
    """Tiny cookie-keeping client for the portal UI (no redirect following)."""

    def __init__(self, port: int) -> None:
        self.port, self.cookie = port, None

    def req(self, method: str, path: str, form: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": self.cookie} if self.cookie else {}
        body = None
        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        set_cookie = r.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        conn.close()
        return r.status, dict(r.getheaders()), data.decode("utf-8", "replace")

    def sign_in(self, login: str, key: str):
        return self.req("POST", "/ui/login", {"login": login, "api_key": key})


@pytest.fixture()
def ui_server(db: Path):
    httpd = serve(db, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ui_sign_in_and_session(db: Path, ui_server: int) -> None:
    d = _dealers(db).iloc[0]
    login = f"{d['dealer_number'].lower()}.admin"
    c = _UI(ui_server)
    status, headers, _ = c.req("GET", "/ui")
    assert status == 303 and headers["Location"] == "/ui/login"
    status, headers, page = c.req("GET", "/ui/login")
    assert status == 200 and 'name="api_key"' in page and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, _, page = c.sign_in(login, "wrong-key")
    assert status == 401 and c.cookie is None and "not valid" in page
    status, headers, _ = c.sign_in(login, demo_api_key(SEED, login))
    assert status == 303 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    status, _, page = c.req("GET", "/ui")
    assert status == 200 and d["dealer_number"] in page
    assert "Updated since" in page and "Access log" in page
    assert c.req("GET", "/ui/static/portal.css")[0] == 200
    assert c.req("GET", "/ui/static/../web.py")[0] in (404, 303)
    stranger = _UI(ui_server)
    stranger.cookie = "portal_session=forged-token"
    assert stranger.req("GET", "/ui")[0] == 303
    token = c.cookie
    assert c.req("POST", "/ui/logout", {"csrf": "wrong"})[0] == 303  # bad CSRF token: the session survives
    c.cookie = token
    status, _, page = c.req("GET", "/ui")
    assert status == 200
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    assert c.req("POST", "/ui/logout", {"csrf": csrf})[0] == 303
    c.cookie = token  # the old cookie no longer works
    assert c.req("GET", "/ui")[0] == 303


def test_ui_denies_another_dealers_data(db: Path, ui_server: int) -> None:
    dealers = _dealers(db)
    d = dealers.iloc[0]
    other = dealers[dealers["dealer_id"] != d["dealer_id"]].iloc[0]
    with sqlite3.connect(db) as conn:
        foreign = {r[0] for r in conn.execute("select contract_number from contract where dealer_id = ?",
                                              [int(other["dealer_id"])])}
    login = f"{d['dealer_number'].lower()}.admin"
    c = _UI(ui_server)
    c.sign_in(login, demo_api_key(SEED, login))
    for path in (f"/ui?dataset=contracts&dealer={other['dealer_number']}",
                 f"/ui/download/contracts?format=csv&dealer={other['dealer_number']}"):
        status, _, page = c.req("GET", path)
        assert status == 403 and not any(n in page for n in foreign)
    status, headers, text = c.req("GET", "/ui/download/contracts?format=csv")
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    got = pd.read_csv(io.StringIO(text), dtype=str)
    assert set(got["dealer_number"]) == {d["dealer_number"]} and not set(got["contract_number"]) & foreign
    # a tenant admin may narrow to its own dealers, never to another tenant's
    admin = f"{d['administrator_code'].lower()}.admin"
    t = _UI(ui_server)
    t.sign_in(admin, demo_api_key(SEED, admin))
    assert t.req("GET", f"/ui?dealer={d['dealer_number']}")[0] == 200
    foreign_tenant = dealers[dealers["tenant_id"] != d["tenant_id"]].iloc[0]["dealer_number"]
    assert t.req("GET", f"/ui?dealer={foreign_tenant}")[0] == 403
    assert t.req("GET", "/ui?scope=contact")[0] == 403  # tenant admins never get the contact scope
    assert 'name="scope"' not in t.req("GET", "/ui")[2]


def test_ui_masks_pii_by_default_and_contact_is_admin_only(db: Path, ui_server: int) -> None:
    dealers = _dealers(db)
    d = dealers.iloc[0]
    with sqlite3.connect(db) as conn:
        emails = {r[0] for r in conn.execute("select customer_email from contract where dealer_id = ? and "
                                             "removed_from_source = 0 order by contract_id limit 100",
                                             [int(d["dealer_id"])]) if r[0]}
    login = f"{d['dealer_number'].lower()}.admin"
    c = _UI(ui_server)
    c.sign_in(login, demo_api_key(SEED, login))
    status, _, page = c.req("GET", "/ui?dataset=contracts")
    assert status == 200 and emails and not any(e in page for e in emails)
    assert "holder_initials" in page and "customer_email" not in page and 'name="scope"' in page
    status, _, text = c.req("GET", "/ui/download/contracts?format=json")
    assert status == 200 and not any(e in text for e in emails)
    status, _, page = c.req("GET", "/ui?dataset=contracts&scope=contact")
    assert status == 200 and "customer_email" in page and any(e in page for e in emails)
    viewer = dealers[dealers["dealer_id"] % 3 == 0].iloc[0]
    vlogin = f"{viewer['dealer_number'].lower()}.viewer"
    v = _UI(ui_server)
    v.sign_in(vlogin, demo_api_key(SEED, vlogin))
    status, _, page = v.req("GET", "/ui")
    assert status == 200 and 'name="scope"' not in page  # no contact toggle for a viewer
    assert v.req("GET", "/ui?scope=contact")[0] == 403
    assert v.req("GET", "/ui/download/contracts?scope=contact")[0] == 403


def test_ui_writes_audit_rows_and_shows_own_history(db: Path, ui_server: int) -> None:
    dealers = _dealers(db)
    d, other = dealers.iloc[0], dealers.iloc[1]
    login = f"{d['dealer_number'].lower()}.admin"
    olog = f"{other['dealer_number'].lower()}.admin"
    o = _UI(ui_server)
    o.sign_in(olog, demo_api_key(SEED, olog))
    assert o.req("GET", "/ui/download/claims?format=csv&since=2026-01-01")[0] == 200
    before = len(_log(db))
    c = _UI(ui_server)
    c.sign_in(login, demo_api_key(SEED, login))
    assert c.req("GET", "/ui?dataset=claims&since=2026-01-01")[0] == 200
    assert c.req("GET", "/ui/download/remittances?format=csv")[0] == 200
    assert c.req("GET", "/ui?since=not-a-date")[0] == 403
    log = _log(db).iloc[before:]
    mine = log[log["login"] == login]
    assert list(mine["status"]) == ["OK", "OK", "DENIED"] and set(mine["channel"]) == {"ui"}
    assert list(mine["dataset"])[:2] == ["claims", "remittances"]
    assert mine.iloc[0]["since"] == "2026-01-01" and set(mine["dealer_number"].dropna()) == {d["dealer_number"]}
    assert mine.iloc[1]["message"] == "download csv" and int(mine.iloc[1]["row_count"]) >= 0
    panel = c.req("GET", "/ui")[2].split("latest 25 reads")[1]
    assert "remittances" in panel and "DENIED" in panel and olog not in panel  # own history only
    assert "operator:" not in panel
    _UI(ui_server).sign_in(login, "nope")
    assert _log(db).iloc[-1][["dataset", "status"]].tolist() == ["sign-in", "DENIED"]
