"""Microsoft's published PBIP / PBIR JSON schemas: download once, cache, validate.

Every JSON file of the report names its schema in ``$schema`` (a developer.microsoft.com URL). The
schemas ``$ref`` each other by relative path, so ``fetch`` follows every reference and caches the whole
set under ``data/powerbi_schemas/`` (git-ignored, like every generated file under ``data/``). Only two
official Microsoft locations are ever contacted: https://developer.microsoft.com/json-schemas/ (PBIP, PBIR)
and the report-theme schema in the microsoft/powerbi-desktop-samples GitHub repository.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urldefrag, urljoin

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("POWERBI_SCHEMA_CACHE", ROOT / "data" / "powerbi_schemas"))
BASE = "https://developer.microsoft.com/json-schemas/"
THEME_BASE = "https://raw.githubusercontent.com/microsoft/powerbi-desktop-samples/main/Report%20Theme%20JSON%20Schema/"
# Theme files carry no $schema; this is the schema of the Desktop release the report was built and opened with.
THEME_SCHEMA = THEME_BASE + "reportThemeSchema-2.159.json"
_BASES = {BASE: "", THEME_BASE: "theme/"}


class SchemaUnavailable(RuntimeError):
    """The schema is not cached and could not be downloaded (offline CI)."""


def _local(url: str) -> Path:
    url = urldefrag(url)[0]
    for base, sub in _BASES.items():
        if url.startswith(base):
            return CACHE / (sub + url[len(base):])
    raise ValueError(f"not an official Microsoft schema URL: {url}")


def _refs(node, base: str, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            out.add(urldefrag(urljoin(base, ref))[0])
        for v in node.values():
            _refs(v, base, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, base, out)


def fetch(url: str, *, offline: bool = False, timeout: float = 30) -> dict:
    """Return the schema at ``url`` (cached), downloading it and everything it references."""
    todo, seen, root = [urldefrag(url)[0]], set(), None
    while todo:
        u = todo.pop()
        if u in seen:
            continue
        seen.add(u)
        path = _local(u)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
        else:
            if offline:
                raise SchemaUnavailable(u)
            try:
                with urllib.request.urlopen(u, timeout=timeout) as r:  # noqa: S310 (fixed https host)
                    raw = r.read().decode("utf-8-sig")
            except OSError as exc:
                raise SchemaUnavailable(f"{u}: {exc}") from exc
            doc = json.loads(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        found: set[str] = set()
        _refs(doc, u, found)
        todo += sorted(found - seen)
        if root is None:
            root = doc
    return root


def validator(url: str, *, offline: bool = False):
    """A jsonschema validator for ``url`` whose $refs resolve from the cache."""
    import jsonschema
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    root = fetch(url, offline=offline)

    def retrieve(uri: str):
        doc = fetch(uri, offline=offline)
        return Resource.from_contents(doc, default_specification=DRAFT7)

    registry = Registry(retrieve=retrieve)
    cls = jsonschema.validators.validator_for(root, default=jsonschema.Draft7Validator)
    return cls(root, registry=registry)
