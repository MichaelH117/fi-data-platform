"""CLI for the read-only raw-data portal.

    python -m portal export --tenant PVA --dealer D1001 [--format csv|json] [--out DIR] [--since YYYY-MM-DD]
                            [--scope standard|contact --login d1001.admin --api-key KEY]
    python -m portal serve [--host 127.0.0.1] [--port 8765]
    python -m portal demo-key --login d1001.admin      # print the synthetic demo API key

``export`` writes one file per dataset (contracts, claims, cancellations, remittances) for that
dealer only, plus ``_export.json`` with row counts. Unknown or foreign tenant/dealer -> exit 2.
The default ``standard`` scope has no customer contact data (initials and masked VINs only, see
``portal.query``); ``--scope contact`` needs the login and API key of that dealer's DEALER_ADMIN.
Every export (and every refused one) is written to the portal access log (``portal.audit``).
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from generator import ADMIN_CORE_PATH, ROOT
from portal import audit, query

log = logging.getLogger("portal")


def export(db: Path, tenant: str, dealer: str | None, fmt: str, out: Path | None, since: str | None,
           data_scope: str = "standard", login: str | None = None, api_key: str | None = None) -> Path:
    who, role = login or f"operator:{getpass.getuser()}", None
    try:
        with query.connect(db) as conn:
            scope = query.resolve(conn, tenant, dealer)
            if scope.dealer_id is None:
                raise query.AccessDenied("a dealer is required for an export")
            if login or data_scope != "standard":
                user_scope, role = query.authenticate(conn, login or "", api_key or "")
                if user_scope.tenant_id != scope.tenant_id:
                    raise query.AccessDenied("unknown tenant or dealer")
                user_scope = query.narrow(conn, user_scope, scope.dealer_number)
                query.check_data_scope(role, user_scope, data_scope)
    except query.AccessDenied as exc:
        audit.record(db, channel="cli", login=who, role=role, tenant_code=tenant, dealer_number=dealer,
                     data_scope=data_scope, dataset="*", row_count=None, since=since, status="DENIED",
                     message=str(exc))
        raise
    with query.connect(db) as conn:
        out = out or ROOT / "data" / "portal" / scope.tenant_code / scope.dealer_number
        out.mkdir(parents=True, exist_ok=True)
        counts = {}
        for name in query.DATASETS:
            df = query.fetch(conn, scope, name, since, data_scope)
            audit.record(db, channel="cli", login=who, role=role, tenant_code=scope.tenant_code,
                         dealer_number=scope.dealer_number, data_scope=data_scope, dataset=name, row_count=len(df),
                         since=since)
            path = out / f"{name}.{fmt}"
            if fmt == "csv":
                df.to_csv(path, index=False, lineterminator="\n")
            else:
                path.write_text(df.to_json(orient="records", date_format="iso", indent=1), encoding="utf-8")
            counts[name] = len(df)
    meta = {"tenant": scope.tenant_code, "dealer": scope.dealer_number, "format": fmt, "since": since,
            "scope": data_scope,
            "exported_at": datetime.now().isoformat(timespec="seconds"), "rows": counts}
    (out / "_export.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("exported %s/%s to %s: %s", scope.tenant_code, scope.dealer_number, out, counts)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portal", description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin-core", type=Path, default=ADMIN_CORE_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="export one dealer's raw contract data")
    ex.add_argument("--tenant", required=True, help="administrator code (e.g. PVA) or tenant id")
    ex.add_argument("--dealer", required=True, help="dealer number (e.g. D1001)")
    ex.add_argument("--format", choices=["csv", "json"], default="csv")
    ex.add_argument("--out", type=Path)
    ex.add_argument("--since", help="only contracts updated on/after this date")
    ex.add_argument("--scope", choices=query.SCOPES, default="standard",
                    help="standard (no customer contact data) or contact (dealer admin only)")
    ex.add_argument("--login", help="portal login (required for --scope contact)")
    ex.add_argument("--api-key", default=os.environ.get("PORTAL_API_KEY"),
                    help="API key of --login (default: env PORTAL_API_KEY)")
    sv = sub.add_parser("serve", help="run the read-only HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    dk = sub.add_parser("demo-key", help="print the synthetic demo API key of a portal login")
    dk.add_argument("--login", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.cmd == "export":
        try:
            export(args.admin_core, args.tenant, args.dealer, args.format, args.out, args.since, args.scope,
                   args.login, args.api_key)
        except query.AccessDenied as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "demo-key":
        from generator.build import demo_api_key, load_state

        with query.connect(args.admin_core) as conn:
            print(demo_api_key(load_state(conn)["seed"], args.login))
        return 0
    from portal.server import serve

    httpd = serve(args.admin_core, args.host, args.port)
    log.info("raw-data portal on http://%s:%d (read-only; Ctrl+C to stop)", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
