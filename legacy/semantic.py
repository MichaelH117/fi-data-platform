"""Legacy semantic-model sprawl: one TMDL-style model per source schema (the ANTI-PATTERN).

``python -m legacy semantic`` writes ``legacy/semantic_models/<Name>.SemanticModel/definition/`` for three
models, each bound to exactly one legacy schema. They are kept to show what the modern platform replaced:

- every model carries its own copy of the shared dimensions (contracts, dealers, products), so the same
  "dimension" has three shapes and three refresh paths;
- the same measure name has different definitions per model (``Claims Paid`` is negative in Ops and
  positive in Reporting; ``Coverage Cost`` comes from three different lineages);
- a filter on "Producers" (Ops: the selling dealer) and "Producer" (Reporting: the provider entity) looks
  the same in a report and means two different things.

The modern fix is ONE shared conformed model (``semantic/``) feeding thin domain models
(docs/V3_DESIGN.md 8.5). These files are never deployed; nothing reads them but the docs and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from legacy import SEMANTIC_DIR

BANNER = "LEGACY ANTI-PATTERN: one semantic model per source schema. Kept for comparison, never deployed."


@dataclass(frozen=True)
class Table:
    name: str
    source: str  # view or table in the model's schema
    columns: list[tuple[str, str]]  # (column, dataType)
    measures: list[tuple[str, str, str]] = field(default_factory=list)  # (name, DAX, format)


@dataclass(frozen=True)
class Model:
    name: str
    schema: str
    database: str
    purpose: str
    tables: list[Table]
    relationships: list[tuple[str, str, str, str]]  # (from table, from column, to table, to column)


INT, S, D, N = "int64", "string", "dateTime", "decimal"
MONEY, WHOLE = "\\$#,0.00;(\\$#,0.00);\\$#,0.00", "#,0"

MODELS: list[Model] = [
    Model("Ops", "legacy_ops", "vsc_legacy", "Dealer and customer reporting off the ops views.", [
        Table("Contracts", "vw_dim_contracts", [
            ("ContractId", INT), ("ContractNumber", S), ("fxDealerId", INT), ("fxProductId", INT), ("EnteredDate", D),
            ("CurrentContractStatus", S), ("ContractPurchasePrice", N), ("Producers", S), ("ProviderCode", S),
            ("Reportable State", S), ("ProductType", S), ("FullName", S), ("EnteredByUser", S)],
            [("Contracts", "COUNTROWS(Contracts)", WHOLE),
             ("Written Premium", "SUM(Contracts[ContractPurchasePrice])", MONEY),
             ("In Force", 'CALCULATE([Contracts], Contracts[CurrentContractStatus] = "INFORCE")', WHOLE)]),
        Table("Dealers", "vw_dim_dealers", [("DealerId", INT), ("Dealer Name", S), ("Number", S), ("Status", S),
                                            ("PhysicalState", S)]),
        Table("Products", "vw_dim_products", [("ProductId", INT), ("ProductCode", S), ("Product", S),
                                              ("ProductType", S), ("PlanYear", INT)]),
        Table("Claims", "vw_dim_claims", [("ClaimId", INT), ("ClaimNumber", S), ("fxContractId", INT),
                                          ("Claim Status", S), ("Claim Substatus", S), ("PaidDate", D),
                                          ("ClaimAmount", N)],
              [("Claims Paid", "SUM(Claims[ClaimAmount])", MONEY)]),
        Table("Cancels", "vw_dim_cancels", [("CancelId", INT), ("fxContractId", INT), ("CancelDate", D),
                                            ("CancelReason", S), ("CustomerRefund", N)],
              [("Cancel Refunds", "SUM(Cancels[CustomerRefund])", MONEY)]),
    ], [("Contracts", "fxDealerId", "Dealers", "DealerId"), ("Contracts", "fxProductId", "Products", "ProductId"),
        ("Claims", "fxContractId", "Contracts", "ContractId"), ("Cancels", "fxContractId", "Contracts", "ContractId")]),
    Model("Finance", "legacy_finance", "vsc_legacy", "Reinsurance and earnings off the finance cubes.", [
        Table("Fact Contracts", "vw_fact_contracts", [("fxContractId", INT), ("fxDealerId", INT), ("fxProductId", INT),
                                                      ("EnteredDate", D), ("GrossPremium", N), ("CoverageCost", N)],
              [("Coverage Cost", "SUM('Fact Contracts'[CoverageCost])", MONEY),
               ("Contracts", "DISTINCTCOUNT('Fact Contracts'[fxContractId])", WHOLE)]),
        Table("Fact Contracts Gen2", "vw_fact_contracts_gen2", [("fxContractId", INT), ("fxDealerId", INT),
                                                                ("EnteredDate", D), ("CoverageCost", N)],
              [("Coverage Cost Gen2", "SUM('Fact Contracts Gen2'[CoverageCost])", MONEY)]),
        Table("Fact Cancels Gen3", "vw_fact_cancels_gen3", [("fxContractId", INT), ("CancelDate", D),
                                                            ("RefundAmount", N)],
              [("Cancel Refunds", "SUM('Fact Cancels Gen3'[RefundAmount])", MONEY)]),
        Table("Earned Disbursement", "cube_earned_disbursement", [("fxContractId", INT), ("fxDisbursementTypeId", INT),
                                                                  ("fxEarningDateId", INT), ("EarnedAmount", N)],
              [("Earned", "SUM('Earned Disbursement'[EarnedAmount])", MONEY)]),
        Table("Cession Type", "vw_dim_cession_type", [("CessionTypeId", INT), ("CessionType", S)]),
    ], [("Earned Disbursement", "fxContractId", "Fact Contracts", "fxContractId")]),
    Model("Reporting", "legacy_reporting", "vsc_legacy", "Provider-scoped dashboards off the reporting layer.", [
        Table("Contracts", "vw_dim_contracts", [("ContractId", INT), ("ContractNumber", S),
                                                ("CurrentContractStatus", S),
                                                ("EnteredDate", D), ("Reportable State", S), ("fxProducerId", INT),
                                                ("CoverageCost", N)],
              [("Coverage Cost", "SUM(Contracts[CoverageCost])", MONEY)]),
        Table("Producers", "vw_dim_producers", [("ProducerId", INT), ("Producer", S)]),
        Table("Product", "vw_dim_product", [("ProductKey", INT), ("Product Code", S), ("Product", S),
                                            ("Plan Year", INT)]),
        Table("Fact Contracts", "vw_fact_contracts", [("fxContractId", INT), ("fxProducerId", INT),
                                                      ("fxProductKey", INT),
                                                      ("EnteredDateId", INT)],
              [("Contracts", "DISTINCTCOUNT('Fact Contracts'[fxContractId])", WHOLE)]),
        Table("Fact Claims", "vw_fact_claims", [("fxContractId", INT), ("fxProducerId", INT), ("Claim Number", S),
                                                ("Claim Amount", N), ("Created Date", D)],
              [("Claims Paid", "SUM('Fact Claims'[Claim Amount])", MONEY)]),
    ], [("Fact Contracts", "fxContractId", "Contracts", "ContractId"),
        ("Fact Contracts", "fxProducerId", "Producers", "ProducerId"),
        ("Fact Contracts", "fxProductKey", "Product", "ProductKey"),
        ("Fact Claims", "fxContractId", "Contracts", "ContractId")]),
]


def _name(s: str) -> str:
    return f"'{s}'" if any(ch in s for ch in " /-") else s


def table_tmdl(m: Model, t: Table) -> str:
    out = [f"/// {BANNER}", f"/// Bound to {m.schema}.{t.source}.", f"table {_name(t.name)}",
           f"\tlineageTag: legacy-{m.name.lower()}-{t.name.lower().replace(' ', '-')}", ""]
    for name, dax, fmt in t.measures:
        out += [f"\tmeasure {_name(name)} = {dax}", f"\t\tformatString: {fmt}", ""]
    for col, dtype in t.columns:
        out += [f"\tcolumn {_name(col)}", f"\t\tdataType: {dtype}", f"\t\tsourceColumn: {col}",
                "\t\tsummarizeBy: none", ""]
    out += [f"\tpartition {_name(t.name)} = m", "\t\tmode: import", "\t\tsource =",
            "\t\t\t\tlet",
            f'\t\t\t\t    Source = PostgreSQL.Database(LegacyServer, "{m.database}"),',
            f'\t\t\t\t    Data = Source{{[Schema="{m.schema}", Item="{t.source}"]}}[Data]',
            "\t\t\t\tin", "\t\t\t\t    Data", ""]
    return "\n".join(out)


def model_files(m: Model) -> dict[str, str]:
    files = {
        "database.tmdl": f"database {m.name}\n\tcompatibilityLevel: 1601\n",
        "model.tmdl": "\n".join([
            f"/// {BANNER}", f"/// {m.purpose} Bound to ONE schema: {m.schema}.", "model Model",
            "\tculture: en-US", f"\tannotation legacy_schema = {m.schema}",
            "\tannotation legacy_anti_pattern = model-per-schema; duplicated dims; conflicting measures",
            "\tannotation generated_by = python -m legacy semantic", "",
            *[f"ref table {_name(t.name)}" for t in m.tables], ""]),
        "expressions.tmdl": 'expression LegacyServer = "legacy-postgres:5432" meta [IsParameterQuery = true, '
                            'Type = "Text", IsParameterQueryRequired = true]\n',
        "relationships.tmdl": "\n".join(
            f"relationship legacy-{m.name.lower()}-{i}\n\tfromColumn: {_name(a)}.{_name(b)}\n"
            f"\ttoColumn: {_name(c)}.{_name(d)}\n" for i, (a, b, c, d) in enumerate(m.relationships, 1)),
    }
    for t in m.tables:
        files[f"tables/{t.name}.tmdl"] = table_tmdl(m, t)
    return {f"{m.name}.SemanticModel/definition/{k}": v for k, v in files.items()}


def conflicts() -> dict[str, list[tuple[str, str]]]:
    """measure name -> [(model, DAX)] for names defined differently in more than one model."""
    seen: dict[str, list[tuple[str, str]]] = {}
    for m in MODELS:
        for t in m.tables:
            for name, dax, _ in t.measures:
                seen.setdefault(name, []).append((m.name, dax))
    return {k: v for k, v in seen.items() if len({d for _, d in v}) > 1}


def duplicated_dims() -> dict[str, list[str]]:
    """Dimension concept -> the models that each keep their own copy of it."""
    concept = {"vw_dim_contracts": "contract", "vw_dim_dealers": "dealer", "vw_dim_products": "product",
               "vw_dim_product": "product", "vw_fact_contracts": "contract facts"}
    out: dict[str, list[str]] = {}
    for m in MODELS:
        for t in m.tables:
            if t.source in concept:
                out.setdefault(concept[t.source], []).append(f"{m.name}:{m.schema}.{t.source}")
    return {k: v for k, v in out.items() if len(v) > 1}


def readme() -> str:
    lines = ["# Legacy semantic models (anti-pattern, generated)", "", f"> {BANNER}", "",
             "Generated by `python -m legacy semantic` from `legacy/semantic.py`. Do not edit by hand.", "",
             "| Model | Bound to schema | Tables | Measures |", "|---|---|---|---|"]
    for m in MODELS:
        lines.append(f"| {m.name} | `{m.schema}` | {len(m.tables)} | {sum(len(t.measures) for t in m.tables)} |")
    lines += ["", "## Same measure name, different definition", "", "| Measure | Model | DAX |", "|---|---|---|"]
    for name, defs in sorted(conflicts().items()):
        lines += [f"| {name} | {mod} | `{dax}` |" for mod, dax in defs]
    lines += ["", "## Dimensions kept once per model", ""]
    lines += [f"- **{k}**: " + ", ".join(f"`{x}`" for x in v) for k, v in sorted(duplicated_dims().items())]
    lines += ["", "## The trap in one line", "",
              "Ops `Contracts[Producers]` is the selling dealer; Reporting `Producers[Producer]` is the provider",
              "entity. A report filter on the wrong one returns zero rows without an error.", "",
              "The modern platform replaces all three with one shared conformed model (`semantic/`).", ""]
    return "\n".join(lines)


def render() -> dict[str, str]:
    files = {"README.md": readme()}
    for m in MODELS:
        files |= model_files(m)
    return files


def write(root: Path = SEMANTIC_DIR) -> list[Path]:
    written = []
    for rel, text in render().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        written.append(p)
    return written
