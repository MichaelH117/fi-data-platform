"""Tiny TMDL writer: tab-indented blocks, the way Tabular Editor and PBIP save a model.

Only the subset this project needs: tables, columns, measures, hierarchies, partitions (M and
calculated), refresh policies, a calculation group, relationships, roles and shared expressions.
"""

from __future__ import annotations

from semantic.metrics import q

TAB = "\t"


def name(n: str) -> str:
    """Quote a TMDL object name when it is not a bare identifier."""
    return n if n.replace("_", "").isalnum() and not n[0].isdigit() else q(n)


def block(text: str, depth: int) -> list[str]:
    """A multi-line expression body, indented one level deeper than its owner."""
    pad = TAB * depth
    return [pad + line if line else "" for line in text.strip("\n").split("\n")]


def prop_expr(key: str, value: str, depth: int) -> list[str]:
    """``key = <expr>``: inline when it is one line, otherwise a block on the next lines."""
    pad = TAB * depth
    if "\n" not in value:
        return [f"{pad}{key} = {value}"]
    return [f"{pad}{key} =", *block(value, depth + 2)]


def description(text: str, depth: int) -> list[str]:
    return [f"{TAB * depth}/// {line}" for line in text.strip().split("\n")] if text else []


def annotation(key: str, value: str, depth: int) -> str:
    return f"{TAB * depth}annotation {key} = {value}"


def join(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"
