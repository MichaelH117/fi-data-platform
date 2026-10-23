"""Brand scrub: ``python tools/brand_scrub.py [--terms PATH] [--root PATH]``.

The repo is public and must stay brand-free: no real company, vendor, system, person or dealer
names. The forbidden-term list lives outside version control (``_private/forbidden_terms.txt``,
git-ignored) so the list itself never leaks. This script walks every text file in the repo,
skipping private notes, the virtualenv, generated data and dbt build output, and reports each hit
as ``path:line: term``. File and folder names are checked too.

Term file format: one term per line, ``#`` starts a comment. Matching is case-insensitive on word
boundaries; a ``cs:`` prefix makes a term case-sensitive (for short acronyms that collide with
normal words). Exit code: 0 clean, 1 hits found, 2 term list missing.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TERMS = REPO_ROOT / "_private" / "forbidden_terms.txt"

# Directories never scanned (relative to the repo root, forward slashes). Prefix match on parts.
SKIP_DIRS = {"_private", ".venv", "venv", "data", ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
             "node_modules", "dbt/target", "dbt/logs", "dbt/dbt", "dbt/dbt_packages"}
SKIP_DIR_PREFIXES = ("dbt/target_", "dbt/logs_")
# Git-ignored build output (rebuilt by ``python -m reports``). Its dealer and customer names come from
# a fake-name library, so they are synthetic; scan it on demand with ``--include-generated``.
GENERATED_DIRS = {"site"}
BINARY_SUFFIXES = {".duckdb", ".db", ".sqlite", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                   ".pdf", ".zip", ".gz", ".parquet", ".pbix", ".pyc", ".woff", ".woff2", ".wal"}
MAX_BYTES = 20_000_000


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    term: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.term}"


def load_terms(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    terms: list[tuple[str, re.Pattern[str]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        flags = re.IGNORECASE
        if line.startswith("cs:"):
            line, flags = line[3:].strip(), 0
        # Word boundaries that also work for terms starting/ending with punctuation (e.g. "A&B Co").
        terms.append((line, re.compile(rf"(?<![A-Za-z0-9]){re.escape(line)}(?![A-Za-z0-9])", flags)))
    return terms


def _skipped(rel: str, skip: set[str] = SKIP_DIRS) -> bool:
    if rel in skip or rel.split("/")[-1] in {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"}:
        return True
    return rel.startswith(SKIP_DIR_PREFIXES) or any(rel.startswith(d + "/") for d in skip)


def iter_files(root: Path, include_generated: bool = False) -> Iterator[Path]:
    skip = SKIP_DIRS if include_generated else SKIP_DIRS | GENERATED_DIRS
    stack = [root]
    while stack:
        folder = stack.pop()
        for entry in sorted(folder.iterdir()):
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                if not _skipped(rel, skip):
                    stack.append(entry)
            elif entry.is_file() and not _skipped(rel, skip):
                yield entry


def scan(root: Path, terms: Sequence[tuple[str, re.Pattern[str]]], include_generated: bool = False) -> list[Hit]:
    hits: list[Hit] = []
    for path in iter_files(root, include_generated):
        rel = path.relative_to(root).as_posix()
        for term, pattern in terms:
            if pattern.search(rel):
                hits.append(Hit(rel, 0, term))
        if path.suffix.lower() in BINARY_SUFFIXES or path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for term, pattern in terms:
            for match in pattern.finditer(text):
                hits.append(Hit(rel, text.count("\n", 0, match.start()) + 1, term))
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--terms", type=Path, default=DEFAULT_TERMS)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--include-generated", action="store_true", help="also scan site/ (git-ignored build output)")
    args = parser.parse_args(argv)
    if not args.terms.exists():
        print(f"brand_scrub: term list not found: {args.terms}", file=sys.stderr)
        return 2
    terms = load_terms(args.terms)
    hits = scan(args.root.resolve(), terms, args.include_generated)
    for hit in hits:
        print(hit)
    print(f"brand_scrub: {len(terms)} terms, {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
