"""CLI: ``python -m publish [--warehouse PATH] [--out DIR] [--packets DIR]``.

Writes the aggregate-only data product to ``data/publish/``:
  peer_benchmark_monthly.csv   state x tier x term band x month, suppressed
  peer_benchmark_t12.csv       same cells over the trailing 12 complete months, suppressed
  manifest.json                rules, window, row and suppression counts
and one branded packet per reportable dealer to ``data/publish/dealers/<tenant_code>/<dealer_number>/``
(``publish/packets.py``; override with ``PACKETS_DIR`` or ``--packets``).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from publish import PACKETS_DIR, PUBLISH_DIR, WAREHOUSE_PATH
from publish.build import DIMS, benchmark_window, build_benchmarks
from publish.packets import build_packets
from publish.suppression import Rules

log = logging.getLogger("publish")


def run(warehouse: Path, out_dir: Path, rules: Rules = Rules(), packets_dir: Path | None = None) -> dict[str, object]:
    with duckdb.connect(str(warehouse), read_only=True) as con:
        tables = build_benchmarks(con, rules)
        window = benchmark_window(con)
        dealer_packets = build_packets(con, packets_dir or out_dir / "dealers", peer=tables.get("peer_benchmark_t12"))
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "last_published_month": f"{window.last_month:%Y-%m}",
        "grain": [*DIMS, "period"],
        "rules": {
            "min_contracts": rules.min_contracts,
            "min_dealers": rules.min_dealers,
            "max_dealer_share": rules.max_dealer_share,
            "complementary": "every margin group has 0 or >=2 hidden cells, plus an exact-disclosure audit",
        },
        "tables": {},
        "dealer_packets": len(dealer_packets),
    }
    for name, df in tables.items():
        path = out_dir / f"{name}.csv"
        df.to_csv(path, index=False)
        reasons = df.loc[df["suppressed"], "suppression_reason"].value_counts().to_dict()
        manifest["tables"][name] = {  # type: ignore[index]
            "rows": len(df),
            "suppressed": int(df["suppressed"].sum()),
            "suppressed_by_reason": {k: int(v) for k, v in reasons.items()},
        }
        log.info("%s: %d rows, %d suppressed %s -> %s", name, len(df), df["suppressed"].sum(), reasons, path)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m publish", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--out", type=Path, default=PUBLISH_DIR)
    ap.add_argument("--packets", type=Path, default=PACKETS_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    run(args.warehouse, args.out, packets_dir=args.packets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
