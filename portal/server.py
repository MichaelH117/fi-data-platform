"""Minimal read-only HTTP API for the raw-data portal (stdlib only, local demo).

    GET /health
    GET /api/v1/<dataset>[?dealer=D1001][&since=YYYY-MM-DD][&format=csv][&scope=standard|contact]

``dataset`` is contracts, claims, cancellations or remittances. Callers authenticate with the
``X-Portal-User`` (login) and ``X-Api-Key`` headers; the scope comes from the portal user, never
from the request, so a dealer user can only read its own dealer and a tenant admin only its tenant.
``scope=contact`` (customer name, e-mail, phone, ZIP, full VIN) is only for a DEALER_ADMIN; every
read, allowed or denied, writes one row to the portal access log (``portal.audit``).
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from portal import audit, query

log = logging.getLogger("portal.server")


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()
    conn = query.connect(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "RawDataPortal/1.0"

        def log_message(self, fmt: str, *args) -> None:  # route access logs through logging
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: HTTPStatus, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._send(status, json.dumps({"error": message}).encode())

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(HTTPStatus.OK, b'{"status": "ok"}')
            parts = url.path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "v1"] or parts[2] not in query.DATASETS:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            login, role, scope = self.headers.get("X-Portal-User", ""), None, None
            data_scope = params.get("scope", "standard")
            try:
                with lock:
                    scope, role = query.authenticate(conn, login, self.headers.get("X-Api-Key", ""))
                    scope = query.narrow(conn, scope, params.get("dealer"))
                    query.check_data_scope(role, scope, data_scope)
                    df = query.fetch(conn, scope, parts[2], params.get("since"), data_scope)
            except query.AccessDenied as exc:
                status = HTTPStatus.UNAUTHORIZED if "credentials" in str(exc) else HTTPStatus.FORBIDDEN
                audit.record(db_path, channel="api", login=login[:64] or "-", role=role,
                             tenant_code=scope.tenant_code if scope else None,
                             dealer_number=(params.get("dealer") or (scope.dealer_number if scope else None)),
                             data_scope=data_scope[:20], dataset=parts[2], row_count=None,
                             since=params.get("since"), status="DENIED", message=str(exc))
                return self._error(status, str(exc))
            audit.record(db_path, channel="api", login=login, role=role, tenant_code=scope.tenant_code,
                         dealer_number=scope.dealer_number, data_scope=data_scope, dataset=parts[2],
                         row_count=len(df), since=params.get("since"))
            if params.get("format") == "csv":
                return self._send(HTTPStatus.OK, df.to_csv(index=False).encode(), "text/csv; charset=utf-8")
            body = {"tenant": scope.tenant_code, "dealer": scope.dealer_number, "dataset": parts[2],
                    "scope": data_scope,
                    "rows": len(df), "data": json.loads(df.to_json(orient="records", date_format="iso"))}
            return self._send(HTTPStatus.OK, json.dumps(body).encode())

    return Handler


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(db_path))
