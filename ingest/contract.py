"""Load a data contract (YAML) and validate a batch against it.

Used for the legacy admin extract (``contracts/legacy_admin.yml``) and the dealer-integration
network landing files (``contracts/feed_*.yml``). Column rules: ``type``, ``required``, ``pii``,
``accepted_values`` (+ ``strict: true`` to quarantine instead of warn) and ``pattern`` (regex,
quarantines on mismatch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

DUCKDB_TYPES = {"string": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP"}
_FORMATS = {"date": "%Y-%m-%d", "timestamp": "%Y-%m-%d %H:%M:%S"}


class SchemaViolation(Exception):
    """The batch cannot be loaded at all (e.g. a required column is missing)."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    accepted_values: tuple[str, ...] | None = None
    strict: bool = False
    pattern: str | None = None


@dataclass(frozen=True)
class TableContract:
    name: str
    primary_key: tuple[str, ...]
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class ValidationResult:
    valid: pd.DataFrame
    quarantined: pd.DataFrame  # raw rows + a ``_reason`` column
    warnings: list[str] = field(default_factory=list)
    row_reasons: pd.Series | None = None  # one entry per input row ("" = valid), input order


def load_contract(path: Path) -> dict[str, TableContract]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    tables = {}
    for name, t in spec["tables"].items():
        cols = []
        for col, c in t["columns"].items():
            if c["type"] not in DUCKDB_TYPES:
                raise ValueError(f"{name}.{col}: unknown type {c['type']!r}")
            accepted = tuple(c["accepted_values"]) if "accepted_values" in c else None
            cols.append(Column(col, c["type"], bool(c.get("required")), bool(c.get("pii")), accepted,
                               bool(c.get("strict")), c.get("pattern")))
        tables[name] = TableContract(name, tuple(t["primary_key"]), tuple(cols))
    return tables


def _cast(raw: pd.Series, type_: str) -> pd.Series:
    if type_ == "string":
        return raw.astype("string")
    if type_ in _FORMATS:
        return pd.to_datetime(raw, format=_FORMATS[type_], errors="coerce")
    num = pd.to_numeric(raw, errors="coerce")
    if type_ == "integer":
        whole = num.notna() & (num % 1 != 0)
        return num.mask(whole).astype("Int64")
    return num.astype("float64")


def validate(batch: pd.DataFrame, contract: TableContract) -> ValidationResult:
    """Type-cast ``batch`` to the contract; quarantine rows that break it instead of failing."""
    warnings: list[str] = []
    batch = batch.reset_index(drop=True)
    extra = sorted(set(batch.columns) - set(contract.column_names))
    if extra:
        warnings.append(f"undeclared columns dropped: {extra}")
    missing = [c for c in contract.columns if c.name not in batch.columns]
    if any(c.required for c in missing):
        raise SchemaViolation(f"{contract.name}: required columns missing: {[c.name for c in missing if c.required]}")
    if missing:
        warnings.append(f"optional columns missing, loaded as NULL: {[c.name for c in missing]}")

    typed = pd.DataFrame(index=batch.index)
    reasons = pd.Series("", index=batch.index)
    for col in contract.columns:
        raw = batch[col.name] if col.name in batch else pd.Series(None, index=batch.index, dtype="object")
        is_blank = raw.isna() | (raw.astype("string").str.strip() == "")
        typed[col.name] = _cast(raw, col.type)
        bad_type = ~is_blank & typed[col.name].isna()
        reasons[bad_type] += f"bad_{col.type}:{col.name};"
        if col.required:
            reasons[is_blank] += f"required_null:{col.name};"
        if col.accepted_values:
            off = ~is_blank & ~raw.isin(col.accepted_values)
            if off.any() and col.strict:
                reasons[off] += f"not_accepted:{col.name};"
            elif off.any():
                warnings.append(f"{col.name}: {int(off.sum())} values outside accepted_values")
        if col.pattern:
            bad_pat = ~is_blank & ~raw.astype("string").str.fullmatch(col.pattern).fillna(False).astype(bool)
            reasons[bad_pat] += f"bad_pattern:{col.name};"

    bad = reasons != ""
    quarantined = batch[bad].copy()
    quarantined["_reason"] = reasons[bad].str.rstrip(";")
    return ValidationResult(
        typed[~bad].reset_index(drop=True), quarantined.reset_index(drop=True), warnings, reasons.str.rstrip(";")
    )
