"""Admin Core booking rules, shared by the generator (history) and ``python -m ingest.feed`` (promotion).

Each function turns business events into the rows Admin Core writes (contract, transaction,
disbursement, commission, status history, ...). The functions are pure: they take frames and
an :class:`Ids` allocator and return frames, so the initial build (one big batch) and the daily
feed promotion (small batches) produce identical shapes.
"""

from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from generator import admin_core as ac
from generator import catalog as cat
from generator import sim
from generator.reference import INSURER_BY_OBLIGOR, claim_grace_days, obligor_for_state

DISB_TYPES = ["ADMIN_FEE", "INSURER_FEE", "PREMIUM_TAX", "COMMISSION", "CLAIMS_RESERVE", "PROFIT_SHARE_RESERVE"]
RESERVE_TO_ENTITY = {"CFC", "NCFC", "PORC", "RETRO", "DEALER_OWNED_OBLIGOR"}
CLASS_BY_MAKE = {m: v[0] for m, v in cat.VEHICLES.items()}
SPECIALTY_CLASS = {"RV": "RV", "PSP": "PS"}  # specialty lines rate by product, not by vehicle make


class Ids:
    """Hands out integer surrogate keys per table (from the database, or from 1 for a fresh build)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn, self.next = conn, {}

    def take(self, table: str, n: int) -> np.ndarray:
        if table not in self.next:
            self.next[table] = ac.next_id(self.conn, table) if self.conn is not None else 1
        out = np.arange(self.next[table], self.next[table] + n)
        self.next[table] += n
        return out


@dataclass
class Ref:
    dealer: pd.DataFrame
    agent: pd.DataFrame
    plan: pd.DataFrame  # joined with product + subcategory
    program: pd.DataFrame
    rate: pd.DataFrame
    comp: pd.DataFrame  # wide: rate_id x disbursement type amounts + refundable flags
    lienholder: pd.DataFrame
    holder: pd.DataFrame
    entity: pd.DataFrame

    @classmethod
    def from_tables(cls, t: dict[str, pd.DataFrame]) -> Ref:
        live = {k: (v[v["removed_from_source"] == 0] if "removed_from_source" in v else v) for k, v in t.items()}
        sub = live["product_subcategory"][["subcategory_id", "subcategory_code"]]
        prod = live["product"].merge(sub, on="subcategory_id")
        plan = live["plan"].drop(columns=["created_at", "updated_at"], errors="ignore").merge(
            prod[["product_id", "product_code", "contract_prefix", "plan_year", "is_cancellable", "claim_wait_days",
                  "claim_wait_miles", "coverage_limit", "subcategory_code", "program_type"]], on="product_id")
        comp = live["rate_component"]
        wide = comp.pivot_table(index="rate_id", columns="disbursement_type", values="amount", aggfunc="sum")
        refundable = comp.pivot_table(index="rate_id", columns="disbursement_type", values="is_refundable",
                                      aggfunc="max").add_suffix("__ref")
        wide = wide.join(refundable).reset_index()
        return cls(
            dealer=live["dealer"], agent=live["agent"], plan=plan, program=live["program"],
            rate=live["rate"][["rate_id", "plan_id", "term_months", "term_miles", "odometer_band", "class_code",
                               "coverage_cost", "suggested_retail"]],
            comp=wide, lienholder=live["lienholder"], holder=live["account_holder"],
            entity=live["participation_entity"])

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Ref:
        names = ["dealer", "agent", "plan", "product", "product_subcategory", "program", "rate", "rate_component",
                 "lienholder", "account_holder", "participation_entity"]
        return cls.from_tables({n: ac.read(conn, n) for n in names})

    def holder_id(self, kind: str, ref_ids) -> np.ndarray:
        h = self.holder[self.holder["holder_type"] == kind].set_index("ref_id")["account_holder_id"]
        return pd.Series(ref_ids).map(h).to_numpy()


def _surcharge_total(codes: pd.Series, table: dict[str, float]) -> np.ndarray:
    def total(s) -> float:
        if not isinstance(s, str) or not s:
            return 0.0
        return float(sum(table.get(c, np.nan) for c in s.split("|")))

    return codes.map(total).to_numpy(dtype=float)


def _crc(values) -> np.ndarray:
    return np.array([zlib.crc32(str(v).encode()) for v in values], dtype=np.int64)


SURCHARGE_MISRATE_PER_MILLE = 8  # about 0.8% of surcharged deals are rated at a stale menu price


def surcharge_misrate(deal_ids: pd.Series, sur_amt: pd.Series) -> np.ndarray:
    """Dollar error on the first surcharge of a mis-rated deal (0 for the rest): the dealer's menu
    charged +/- $25-$50 against the catalogue. Deterministic per deal_id, so the dealer file, the
    feed promotion and the bulk build agree, and no random draw is consumed."""
    h = _crc(deal_ids.fillna("").to_numpy())
    hit = (h % 1000 < SURCHARGE_MISRATE_PER_MILLE) & (sur_amt.fillna(0).to_numpy() > 0)
    size = 25.0 + 5.0 * ((h // 1000) % 6)
    sign = np.where((h // 7000) % 2 == 0, 1.0, -1.0)
    return np.where(hit, sign * size, 0.0)


def _explode_codes(df: pd.DataFrame, col: str, table: dict[str, float], out_col: str) -> pd.DataFrame:
    x = df[["contract_id", "tenant_id", "_booked_at", col]].dropna(subset=[col])
    x = x[x[col] != ""]
    x = x.assign(**{out_col: x[col].str.split("|")}).explode(out_col)
    x["amount"] = x[out_col].map(table)
    return x


def rate_deals(deals: pd.DataFrame, ref: Ref) -> tuple[pd.DataFrame, pd.Series]:
    """Resolve dealer, plan, program and rate for each deal. Returns (rated, reject_reason by index)."""
    d = deals.copy()
    reasons = pd.Series("", index=d.index)
    dealer = ref.dealer.set_index("dealer_number")
    known = d["dealer_number"].isin(dealer.index)
    reasons[~known] += "unknown_dealer;"
    active = d["dealer_number"].map(dealer["status"]).eq("ACTIVE")
    reasons[known & ~active] += "dealer_not_active;"
    for col in ["dealer_id", "tenant_id", "agent_id", "state", "program"]:
        d["_d_" + col] = d["dealer_number"].map(dealer[col])
    plan = ref.plan.set_index("plan_code")
    has_plan = d["plan_code"].isin(plan.index)
    reasons[~has_plan] += "unknown_plan;"
    for col in ["plan_id", "product_code", "contract_prefix", "plan_year", "deductible_amount", "deductible_name",
                "claim_wait_days", "claim_wait_miles", "coverage_limit", "subcategory_code", "is_cancellable",
                "program_type"]:
        d["_p_" + col] = d["plan_code"].map(plan[col])
    reasons[has_plan & (d["_p_product_code"] != d["product_code"])] += "plan_product_mismatch;"
    rated = d["_p_subcategory_code"].map(lambda s: cat.SUBCAT_BY_CODE[s].rated_by_vehicle if isinstance(s, str)
                                         else False).astype(bool)
    d["_band"] = np.where(rated, sim.odometer_band(d["odometer"].fillna(0).to_numpy()), "ALL")
    d["_class"] = np.where(rated, d["vehicle_make"].map(CLASS_BY_MAKE).fillna("B"), "ALL")
    key = ["plan_id", "term_months", "odometer_band", "class_code"]
    look = d[["_p_plan_id", "term_months", "_band", "_class"]].set_axis(key, axis=1)
    look["term_months"] = pd.to_numeric(look["term_months"], errors="coerce")
    r = look.reset_index().merge(ref.rate, on=key, how="left").set_index("index").reindex(d.index)
    reasons[has_plan & r["rate_id"].isna()] += "no_rate;"
    d["_rate_id"] = r["rate_id"]
    d["_rate_cost"] = r["coverage_cost"]
    d["_term_miles"] = r["term_miles"]
    d["_suggested_retail"] = r["suggested_retail"]
    d["_sur_amt"] = _surcharge_total(d.get("surcharge_codes", pd.Series(index=d.index, dtype=object)), cat.SURCHARGES)
    d["_sur_adj"] = surcharge_misrate(d.get("deal_id", pd.Series(index=d.index, dtype=object)), d["_sur_amt"])
    d["_sur_amt"] = d["_sur_amt"] + d["_sur_adj"]
    d["_opt_amt"] = _surcharge_total(d.get("option_codes", pd.Series(index=d.index, dtype=object)), cat.OPTIONS)
    reasons[np.isnan(d["_sur_amt"]) | np.isnan(d["_opt_amt"])] += "unknown_surcharge_or_option;"
    d["_coverage_cost"] = (d["_rate_cost"] + d["_sur_amt"] + d["_opt_amt"]).round(2)
    if "coverage_cost" in d:
        off = (pd.to_numeric(d["coverage_cost"], errors="coerce") - d["_coverage_cost"]).abs() > 0.01
        reasons[off & r["rate_id"].notna()] += "coverage_cost_mismatch;"
    prog = ref.program[["plan_id", "program_code", "program_id"]].rename(
        columns={"plan_id": "_p_plan_id", "program_code": "_d_program"})
    d["_program_id"] = d[["_p_plan_id", "_d_program"]].reset_index().merge(
        prog, on=["_p_plan_id", "_d_program"], how="left").set_index("index").reindex(d.index)["program_id"]
    return d, reasons.str.rstrip(";")


def sale_rows(d: pd.DataFrame, ref: Ref, ids: Ids, as_of: np.datetime64) -> dict[str, pd.DataFrame]:
    """Book rated deals (output of :func:`rate_deals`, reject-free) as PENDING contracts.

    ``d`` needs ``_booked_at`` (row timestamp). Writes contract, SALE transaction, six disbursements,
    commission(s), surcharges, options and the first status-history row.
    """
    n = len(d)
    if n == 0:
        return {}
    d = d.copy()
    d["contract_id"] = d["contract_id"] if "contract_id" in d else ids.take("contract", n)
    d["tenant_id"] = d["_d_tenant_id"].astype(int)
    sale = sim.parse_days(d["sale_date"])
    states = d["_d_state"].to_numpy()
    original = np.array([obligor_for_state(s) for s in states])
    moved = np.isin(states, cat.OBLIGOR_TRANSFER["states"])
    xfer_day = np.datetime64(cat.OBLIGOR_TRANSFER["from_date"])
    to = cat.OBLIGOR_TRANSFER["to_obligor"]
    original = np.where(moved & (sale >= xfer_day), to, original)
    current = np.where(moved & (as_of >= xfer_day), to, original)
    lien = ref.lienholder.set_index(["tenant_id", "lienholder_code"])["lienholder_id"]
    term = d["term_months"].astype(int).to_numpy()
    tmiles = d["_term_miles"].to_numpy(dtype=float)
    odo = pd.to_numeric(d["odometer"], errors="coerce").to_numpy(dtype=float)
    booked = d["_booked_at"].to_numpy()
    contract = pd.DataFrame({
        "contract_id": d["contract_id"].to_numpy(),
        "tenant_id": d["tenant_id"].to_numpy(),
        "contract_number": d["contract_number"].to_numpy(),
        "deal_id": d["deal_id"].to_numpy(),
        "dealer_id": d["_d_dealer_id"].astype(int).to_numpy(),
        "agent_id": d["_d_agent_id"].astype(int).to_numpy(),
        "plan_id": d["_p_plan_id"].astype(int).to_numpy(),
        "program_id": d["_program_id"].to_numpy(),
        "rate_id": d["_rate_id"].astype(int).to_numpy(),
        "status": "PENDING",
        "entered_date": sim.iso(sale),
        "effective_date": sim.iso(sale),
        "expiration_date": sim.iso(sim.add_months(sale, term)),
        "expiration_mileage": np.where(np.isnan(tmiles), None, odo + np.nan_to_num(tmiles)),
        "term_months": term,
        "term_miles": np.where(np.isnan(tmiles), None, tmiles),
        "odometer_band": sim.odometer_band(np.nan_to_num(odo)),
        "deductible_amount": d["_p_deductible_amount"].to_numpy(),
        "deductible_name": d["_p_deductible_name"].to_numpy(),
        "claim_wait_days": d["_p_claim_wait_days"].to_numpy(),
        "claim_wait_miles": d["_p_claim_wait_miles"].to_numpy(),
        "coverage_limit": d["_p_coverage_limit"].to_numpy(),
        "purchase_price": pd.to_numeric(d["purchase_price"]).to_numpy(),
        "coverage_cost": d["_coverage_cost"].to_numpy(),
        "retail_price": (d["_suggested_retail"] + d["_sur_amt"] + d["_opt_amt"]).to_numpy(),
        "msrp": d["msrp"].to_numpy(),
        "finance_type": d["finance_type"].to_numpy(),
        "finance_amount": d["finance_amount"].to_numpy(),
        "apr": d["apr"].to_numpy(),
        "new_used": d["new_used"].to_numpy(),
        "odometer": d["odometer"].to_numpy(),
        "vin": d["vin"].to_numpy(),
        "chassis_vin": d["chassis_vin"].to_numpy(),
        "vehicle_year": d["vehicle_year"].to_numpy(),
        "vehicle_make": d["vehicle_make"].to_numpy(),
        "vehicle_model": d["vehicle_model"].to_numpy(),
        "fuel_type": d["fuel_type"].to_numpy(),
        "cylinders": d["cylinders"].to_numpy(),
        "drive_type": d["drive_type"].to_numpy(),
        "is_commercial": d["is_commercial"].to_numpy(),
        "title_brand": d["title_brand"].to_numpy(),
        "class_code": d["_p_subcategory_code"].map(SPECIALTY_CLASS).fillna(d["vehicle_make"].map(CLASS_BY_MAKE))
        .fillna("B").to_numpy(),
        "reportable_state": states,
        "original_obligor_id": original,
        "current_obligor_id": current,
        "insurer_id": [INSURER_BY_OBLIGOR[int(o)] for o in original],
        "lienholder_id": d[["tenant_id", "lienholder_code"]].merge(lien.reset_index(), how="left",
                                                                   on=["tenant_id", "lienholder_code"])[
            "lienholder_id"].to_numpy(),
        **{c: d[c].to_numpy() for c in ["customer_first_name", "customer_last_name", "customer_email",
                                        "customer_phone", "customer_zip", "customer_state"]},
        "program_type": d["_p_program_type"].to_numpy(),
        **header_fields(d.assign(_booked_at=booked), sale),
        "created_at": booked,
        "updated_at": booked,
    })
    d["contract_id"] = contract["contract_id"].to_numpy()
    docs = contract_documents(contract, d["_p_subcategory_code"].to_numpy(), d["_p_plan_year"].to_numpy(), ids, as_of)

    txn = pd.DataFrame({
        "txn_id": ids.take("transaction", n), "tenant_id": contract["tenant_id"],
        "contract_id": contract["contract_id"],
        "txn_type": "SALE", "posted_at": booked, "amount": contract["coverage_cost"], "source_ref": d["deal_id"].values,
        "created_at": booked, "updated_at": booked,
    })
    disb = disbursements(
        contract.assign(_extra=(d["_sur_amt"] + d["_opt_amt"]).to_numpy()), txn, ref, sign=1.0, pct=np.ones(n)
    )
    comm = commissions(contract, txn, ref)
    hist = status_rows(ids, contract["tenant_id"], contract["contract_id"], booked, None, "PENDING")
    d["_booked_at"] = booked
    d["tenant_id"] = contract["tenant_id"].to_numpy()
    sur = _explode_codes(d, "surcharge_codes", cat.SURCHARGES, "surcharge_code")
    if "_sur_adj" in d and len(sur):  # a mis-rated deal carries its error on its first surcharge line
        first = ~sur.index.duplicated(keep="first")
        sur["amount"] = sur["amount"].to_numpy() + d["_sur_adj"].reindex(sur.index).fillna(0).to_numpy() * first
    opt = _explode_codes(d, "option_codes", cat.OPTIONS, "option_code")
    sur = pd.DataFrame({"surcharge_id": ids.take("contract_surcharge", len(sur)), "tenant_id": sur["tenant_id"].values,
                        "contract_id": sur["contract_id"].values, "surcharge_code": sur["surcharge_code"].values,
                        "amount": sur["amount"].values, "created_at": sur["_booked_at"].values,
                        "updated_at": sur["_booked_at"].values})
    opt = pd.DataFrame({"option_id": ids.take("contract_option", len(opt)), "tenant_id": opt["tenant_id"].values,
                        "contract_id": opt["contract_id"].values, "option_code": opt["option_code"].values,
                        "amount": opt["amount"].values, "created_at": opt["_booked_at"].values,
                        "updated_at": opt["_booked_at"].values})
    for frame, table in [(disb, "disbursement"), (comm, "commission")]:
        frame.insert(0, ac.pk(table), ids.take(table, len(frame)))
    return {"contract": contract, "transaction": txn, "disbursement": disb, "commission": comm,
            "contract_surcharge": sur, "contract_option": opt, "contract_status_history": hist,
            "contract_document": docs}

# ---------------------------------------------------------------- admin-screen header fields (V6 edit A)
ESIGNED_PCT = 72  # share of contracts signed electronically (the rest are wet-signed paper jackets)


def header_fields(d: pd.DataFrame, sale: np.ndarray) -> dict[str, np.ndarray]:
    """Contract header fields the admin screen shows that the feed does not send: claim grace days (by
    product), e-signed flag, finalized timestamp (= booking) and the vehicle in-service date (the sale date
    for a new vehicle; for a used one a day in its model year's selling season, before the sale).
    Hash-keyed on the contract number, so no random draw moves and the build and the feed promotion agree."""
    h = _crc(d["contract_number"].to_numpy())
    grace = d["_p_subcategory_code"].map(lambda s: claim_grace_days(cat.SUBCAT_BY_CODE[s])).to_numpy()
    year = pd.to_numeric(d["vehicle_year"], errors="coerce").to_numpy()
    year = np.where(np.isnan(year), sale.astype("M8[Y]").astype(int) + 1970, year).astype(int)
    season = np.array([f"{y - 1}-10-01" for y in year], dtype="M8[D]") + (h % 365) * sim.DAY
    used = d["new_used"].astype(str).str.upper().eq("USED").to_numpy()
    in_service = np.where(used, np.minimum(season, sale - sim.DAY), sale)
    return {"claim_grace_days": grace, "is_esigned": ((h // 13) % 100 < ESIGNED_PCT).astype(int),
            "finalized_at": d["_booked_at"].to_numpy(), "in_service_date": sim.iso(in_service)}


# ---------------------------------------------------------------- contract documents (V6 edit 4)
FORM_CODE_FROM = 2024  # documents generated before this year carry no form_code (the field did not exist)
REGEN_PCT, REGEN_DELETED_PCT, OTHER_PCT = 6, 2, 4  # regenerated CONTRACT doc; of those, deleted again; OTHER doc
TENANT_CODE = {t[0]: t[1] for t in cat.TENANTS}


def _doc_frame(ids: Ids, contract_id, tenant_id, related, kind: str, at, form, name, deleted) -> pd.DataFrame:
    n = len(contract_id)
    return pd.DataFrame({
        "document_id": ids.take("contract_document", n), "tenant_id": np.asarray(tenant_id),
        "contract_id": np.asarray(contract_id), "related_document_id": related, "document_type": kind,
        "generated_at": np.asarray(at), "form_code": form, "document_name": name,
        "is_deleted": np.broadcast_to(np.asarray(deleted, dtype=int), (n,)),
        "created_at": np.asarray(at), "updated_at": np.asarray(at)})


def doc_names(subcat, plan_year, tenant_id, kind: str, version: int) -> np.ndarray:
    """``<FORM>_<KIND> - <ver> - <provider>``, e.g. ``VSCPT_STD - 2024.1 - PVA`` (KIND: STD, CXL, OTH)."""
    return np.array([f"{str(s).replace('-', '')}_{kind} - {int(y)}.{version} - {TENANT_CODE.get(int(t), 'ADM')}"
                     for s, y, t in zip(subcat, plan_year, tenant_id, strict=True)], dtype=object)


def contract_documents(contract: pd.DataFrame, subcat: np.ndarray, plan_year: np.ndarray, ids: Ids,
                       as_of: np.datetime64) -> pd.DataFrame:
    """One CONTRACT document per booked contract at booking time. A few are regenerated 1-20 days later (the
    new one points at the one it replaces via related_document_id, and some regenerations are deleted again,
    so the original wins); a few contracts also get an OTHER document. Hash-keyed: no random draw is used."""
    if contract.empty:
        return pd.DataFrame()
    at = contract["created_at"].astype(str).to_numpy()
    ten = contract["tenant_id"].to_numpy()
    cid = contract["contract_id"].to_numpy()
    h = _crc(contract["contract_number"].to_numpy())
    form_all = np.array([f"{str(s).replace('-', '')}-{int(y) % 100:02d}-{st}" for s, y, st in
                         zip(subcat, plan_year, contract["reportable_state"], strict=True)], dtype=object)

    def form_for(stamps) -> np.ndarray:
        return np.array([int(str(a)[:4]) >= FORM_CODE_FROM for a in stamps], dtype=bool)

    v1 = _doc_frame(ids, cid, ten, None, "CONTRACT", at, np.where(form_for(at), form_all, None),
                    doc_names(subcat, plan_year, ten, "STD", 1), 0)
    regen = np.array([a[:19] for a in at], dtype="M8[s]") + ((1 + (h // 100) % 20) * 86400).astype("m8[s]")
    re = (h % 100 < REGEN_PCT) & (regen < (np.datetime64(as_of, "D") + sim.DAY).astype("M8[s]"))
    re_at = np.array([str(x).replace("T", " ") for x in regen[re]], dtype=object)
    v2 = _doc_frame(ids, cid[re], ten[re], v1["document_id"].to_numpy()[re], "CONTRACT", re_at,
                    np.where(form_for(re_at), form_all[re], None),
                    doc_names(subcat[re], plan_year[re], ten[re], "STD", 2), (h[re] % 100) < REGEN_DELETED_PCT)
    oth = (h // 7) % 100 < OTHER_PCT
    v3 = _doc_frame(ids, cid[oth], ten[oth], v1["document_id"].to_numpy()[oth], "OTHER", at[oth], None,
                    doc_names(subcat[oth], plan_year[oth], ten[oth], "OTH", 1), 0)
    return pd.concat([v1, v2, v3], ignore_index=True)


def cancel_documents(cancel: pd.DataFrame, contract: pd.DataFrame, ref: Ref, ids: Ids) -> pd.DataFrame:
    """One CANCEL document per cancellation, generated when the cancel is processed (no form code)."""
    plan = ref.plan.set_index("plan_id")
    pid = pd.Series(contract["plan_id"].to_numpy())
    ten = cancel["tenant_id"].to_numpy()
    return _doc_frame(ids, cancel["contract_id"].to_numpy(), ten, None, "CANCEL", cancel["processed_at"].to_numpy(),
                      None, doc_names(pid.map(plan["subcategory_code"]).to_numpy(),
                                      pid.map(plan["plan_year"]).fillna(0).to_numpy(), ten, "CXL", 1), 0)


def ceding_entity(dealer_id: pd.Series, entered_date: pd.Series | None, ref: Ref) -> tuple[pd.Series, pd.Series]:
    """(structure_type, entity_id) the dealer's reserve cedes to, per row. A run-off entity takes no contract
    entered after its run-off date (V6 edit 13): those rows get no entity, so the reserve stays with the obligor."""
    ent = ref.entity.set_index("dealer_id")
    dealer_id = dealer_id.reset_index(drop=True)
    structure, entity_id = dealer_id.map(ent["structure_type"]), dealer_id.map(ent["entity_id"])
    if "runoff_date" in ent and entered_date is not None:
        runoff = dealer_id.map(ent["runoff_date"]).to_numpy(dtype=object)
        entered = pd.Series(entered_date).astype(str).to_numpy()
        closed = np.array([isinstance(r, str) and e > r for r, e in zip(runoff, entered, strict=True)], dtype=bool)
        structure, entity_id = structure.where(~closed), entity_id.where(~closed)
    return structure, entity_id


def disbursements(contract: pd.DataFrame, txn: pd.DataFrame, ref: Ref, sign: float, pct: np.ndarray) -> pd.DataFrame:
    """Split each contract's rate components to account holders. ``pct`` scales refundable parts on cancel."""
    c = contract[["contract_id", "tenant_id", "dealer_id", "agent_id", "rate_id", "original_obligor_id",
                  "insurer_id"]].copy()
    c["_extra"] = contract["_extra"].to_numpy() if "_extra" in contract else 0.0
    c["_pct"] = pct
    c["txn_id"] = txn["txn_id"].to_numpy()
    c["posted_at"] = txn["posted_at"].to_numpy()
    c = c.merge(ref.comp, on="rate_id", how="left")
    structure, entity_id = ceding_entity(c["dealer_id"], contract.get("entered_date"), ref)
    reserve_holder = ref.holder_id("DEALER_RESERVE", entity_id)
    admin_holder = ref.holder_id("ADMIN", c["tenant_id"])
    obligor_holder = ref.holder_id("OBLIGOR", c["original_obligor_id"])
    holders = {
        "ADMIN_FEE": admin_holder,
        "INSURER_FEE": ref.holder_id("INSURER", c["insurer_id"]),
        "PREMIUM_TAX": obligor_holder,
        "COMMISSION": ref.holder_id("AGENT", c["agent_id"]),
        "CLAIMS_RESERVE": np.where(structure.isin(RESERVE_TO_ENTITY), reserve_holder, obligor_holder),
        "PROFIT_SHARE_RESERVE": np.where(entity_id.notna(), reserve_holder, admin_holder),
    }
    parts = []
    for kind in DISB_TYPES:
        amt = c[kind].to_numpy(dtype=float) + (c["_extra"].to_numpy(dtype=float) if kind == "CLAIMS_RESERVE" else 0)
        refundable = c[kind + "__ref"].to_numpy(dtype=float)
        scale = np.where(sign < 0, c["_pct"] * refundable, c["_pct"])
        parts.append(pd.DataFrame({
            "tenant_id": c["tenant_id"], "txn_id": c["txn_id"], "contract_id": c["contract_id"],
            "disbursement_type": kind, "account_holder_id": holders[kind].astype("int64"),
            "amount": np.round(sign * amt * scale, 2), "posted_at": c["posted_at"],
            "is_refundable": refundable.astype(int), "is_renewable": int(kind in ("CLAIMS_RESERVE", "ADMIN_FEE")),
            "created_at": c["posted_at"], "updated_at": c["posted_at"],
        }))
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["txn_id", "disbursement_type"], kind="stable").reset_index(drop=True)


def commissions(contract: pd.DataFrame, txn: pd.DataFrame, ref: Ref, scale: np.ndarray | None = None) -> pd.DataFrame:
    """Primary agent gets 100%, or 70/30 with a second agent of the same tenant (chosen by a stable hash)."""
    c = contract[["contract_id", "tenant_id", "agent_id", "rate_id", "contract_number"]].copy()
    c["txn_id"], c["posted_at"] = txn["txn_id"].to_numpy(), txn["posted_at"].to_numpy()
    c["_scale"] = 1.0 if scale is None else scale
    c = c.merge(ref.comp[["rate_id", "COMMISSION"]], on="rate_id", how="left")
    h = _crc(c["contract_number"])
    split = h % 100 < 12
    by_tenant = ref.agent.groupby("tenant_id")["agent_id"].apply(list).to_dict()
    second = [
        [a for a in by_tenant[t] if a != p][int(k) % max(len(by_tenant[t]) - 1, 1)] if len(by_tenant[t]) > 1 else p
        for t, p, k in zip(c["tenant_id"], c["agent_id"], h // 100, strict=True)
    ]
    advance = ref.agent.set_index("agent_id")["is_advance_agreement"]
    base = c["COMMISSION"].to_numpy(dtype=float) * c["_scale"].to_numpy(dtype=float)
    first = pd.DataFrame({
        "tenant_id": c["tenant_id"], "contract_id": c["contract_id"], "agent_id": c["agent_id"],
        "txn_id": c["txn_id"], "posted_at": c["posted_at"], "amount": np.round(base * np.where(split, 0.7, 1.0), 2),
        "share_pct": np.where(split, 0.7, 1.0), "is_advance": c["agent_id"].map(advance).to_numpy(),
    })
    sec = pd.DataFrame({
        "tenant_id": c["tenant_id"], "contract_id": c["contract_id"], "agent_id": second, "txn_id": c["txn_id"],
        "posted_at": c["posted_at"], "amount": np.round(base * 0.3, 2), "share_pct": 0.3,
        "is_advance": pd.Series(second).map(advance).to_numpy(),
    })[split]
    out = pd.concat([first, sec], ignore_index=True).sort_values(["txn_id", "share_pct"], ascending=[True, False])
    out["created_at"] = out["updated_at"] = out["posted_at"]
    return out.reset_index(drop=True)


def status_rows(ids: Ids, tenant, contract_id, at, from_status, to_status) -> pd.DataFrame:
    n = len(contract_id)
    return pd.DataFrame({
        "status_history_id": ids.take("contract_status_history", n), "tenant_id": np.asarray(tenant),
        "contract_id": np.asarray(contract_id), "changed_at": np.asarray(at), "from_status": from_status,
        "to_status": to_status, "created_at": np.asarray(at), "updated_at": np.asarray(at),
    })


def remit_rows(r: pd.DataFrame, ids: Ids) -> dict[str, pd.DataFrame]:
    """Received remittance: ``r`` has remit_batch_id, tenant_id, dealer_id, contract_id, amount,
    entered_at, sent_at, received_at, source. Writes the batch, cash lines, CASH txns, and
    PENDING -> INFORCE history (the caller flips contract.status)."""
    if r.empty:
        return {}
    b = r.drop_duplicates("remit_batch_id").sort_values("remit_batch_id")[
        ["remit_batch_id", "tenant_id", "dealer_id", "entered_at", "sent_at", "received_at", "source"]
    ].reset_index(drop=True)  # one timeline per batch, so the first row speaks for the batch
    b["status"] = np.where(b["received_at"].notna(), "RECEIVED", np.where(b["sent_at"].notna(), "SENT", "ENTERED"))
    b["created_at"] = b["entered_at"]
    b["updated_at"] = b[["entered_at", "sent_at", "received_at"]].astype(str).replace("None", "").max(axis=1)
    got = r[r["received_at"].notna()]
    cash = pd.DataFrame({
        "cash_txn_id": ids.take("cash_transaction", len(got)), "tenant_id": got["tenant_id"].values,
        "remit_batch_id": got["remit_batch_id"].values, "contract_id": got["contract_id"].values,
        "amount": got["amount"].values, "posted_at": got["received_at"].values,
        "created_at": got["received_at"].values, "updated_at": got["received_at"].values,
    })
    txn = pd.DataFrame({
        "txn_id": ids.take("transaction", len(got)), "tenant_id": cash["tenant_id"], "contract_id": cash["contract_id"],
        "txn_type": "CASH", "posted_at": cash["posted_at"], "amount": cash["amount"],
        "source_ref": cash["remit_batch_id"], "created_at": cash["posted_at"], "updated_at": cash["posted_at"],
    })
    hist = status_rows(ids, cash["tenant_id"], cash["contract_id"], cash["posted_at"], "PENDING", "INFORCE")
    return {"remit_batch": b, "cash_transaction": cash, "transaction": txn, "contract_status_history": hist}


def refund_terms(cx: pd.DataFrame) -> pd.DataFrame:
    """Refund math for cancellations. ``cx`` has purchase_price, coverage_cost, admin_fee, term_months,
    contract_age_days, method, withheld (claims withheld)."""
    tdays = sim.term_days(cx["term_months"].to_numpy())
    age = cx["contract_age_days"].to_numpy()
    months = cx["term_months"].to_numpy()
    k = np.minimum(np.ceil(age / 30.4375), months)
    r78 = (months - k) * (months - k + 1) / (months * (months + 1))
    pct = np.select([cx["method"] == "FULL_REFUND", cx["method"] == "R78"], [1.0, r78],
                    np.clip(1 - age / tdays, 0, 1))
    pct = np.round(pct, 4)
    fee = np.where(cx["method"] == "FULL_REFUND", 0.0, cat.CANCEL_FEE)
    refundable_cost = cx["coverage_cost"].to_numpy() - cx["admin_fee"].to_numpy()
    cc_refund = np.round(refundable_cost * pct, 2)
    profit_refund = np.round((cx["purchase_price"].to_numpy() - cx["coverage_cost"].to_numpy()) * pct, 2)
    withheld = np.minimum(cx["withheld"].to_numpy(dtype=float), cc_refund + profit_refund)
    customer = np.round(np.maximum(cc_refund + profit_refund - fee - withheld, 0), 2)
    return pd.DataFrame({
        "refund_pct": pct, "cancel_fee": fee, "customer_refund": customer, "admin_portion": cc_refund,
        "dealer_portion": profit_refund, "coverage_cost_refund": cc_refund, "dealer_profit_refund": profit_refund,
        "admin_withheld_claims": np.round(withheld, 2),
    }, index=cx.index)


def cancel_rows(cx: pd.DataFrame, contract: pd.DataFrame, ref: Ref, ids: Ids) -> dict[str, pd.DataFrame]:
    """``cx``: contract_id, cancel_date, processed_at, initiated_by, reason, method, odometer, withheld,
    from_status. ``contract``: the contract rows (contract table columns)."""
    if cx.empty:
        return {}
    c = contract.set_index("contract_id").loc[cx["contract_id"]].reset_index()
    c = c.merge(ref.comp[["rate_id", "ADMIN_FEE"]], on="rate_id", how="left").rename(columns={"ADMIN_FEE": "admin_fee"})
    eff = sim.parse_days(c["effective_date"])
    age = ((sim.parse_days(cx["cancel_date"]) - eff) / sim.DAY).astype(int)
    terms = refund_terms(pd.DataFrame({
        "purchase_price": c["purchase_price"].values, "coverage_cost": c["coverage_cost"].values,
        "admin_fee": c["admin_fee"].values, "term_months": c["term_months"].values, "contract_age_days": age,
        "method": cx["method"].values, "withheld": cx["withheld"].values}))
    at = cx["processed_at"].to_numpy()
    cancel = pd.DataFrame({
        "cancel_id": ids.take("cancellation", len(cx)), "tenant_id": c["tenant_id"].values,
        "contract_id": c["contract_id"].values, "cancel_date": cx["cancel_date"].values, "processed_at": at,
        "initiated_by": cx["initiated_by"].values, "reason": cx["reason"].values, "method": cx["method"].values,
        "odometer": cx["odometer"].values, "contract_age_days": age, **{k: terms[k].values for k in terms},
        "created_at": at, "updated_at": at,
    })
    txn = pd.DataFrame({
        "txn_id": ids.take("transaction", len(cx)), "tenant_id": cancel["tenant_id"],
        "contract_id": cancel["contract_id"],
        "txn_type": "CANCEL", "posted_at": at, "amount": -cancel["coverage_cost_refund"],
        "source_ref": cancel["cancel_id"].astype(str), "created_at": at, "updated_at": at,
    })
    extra = c["coverage_cost"].to_numpy() - c.merge(ref.rate[["rate_id", "coverage_cost"]], on="rate_id",
                                                    suffixes=("", "_r"))["coverage_cost_r"].to_numpy()
    disb = disbursements(c.assign(_extra=extra), txn, ref, sign=-1.0, pct=terms["refund_pct"].to_numpy())
    disb.insert(0, "disbursement_id", ids.take("disbursement", len(disb)))
    comm = commissions(c, txn, ref, scale=-terms["refund_pct"].to_numpy())
    comm.insert(0, "commission_id", ids.take("commission", len(comm)))
    hist = status_rows(ids, cancel["tenant_id"], cancel["contract_id"], at, cx["from_status"].values, "CANCELLED")
    return {"cancellation": cancel, "transaction": txn, "disbursement": disb, "commission": comm,
            "contract_status_history": hist, "contract_document": cancel_documents(cancel, c, ref, ids)}


def reinstate_rows(rx: pd.DataFrame, cancel_disb: pd.DataFrame, cancel_comm: pd.DataFrame, ids: Ids
                   ) -> dict[str, pd.DataFrame]:
    """``rx``: cancel_id, contract_id, tenant_id, coverage_cost_refund, reinstated_at, status.
    APPROVED reverses the cancel's transaction, disbursements and commission chargebacks."""
    if rx.empty:
        return {}
    at = rx["reinstated_at"].to_numpy()
    rein = pd.DataFrame({
        "reinstatement_id": ids.take("reinstatement", len(rx)), "tenant_id": rx["tenant_id"].values,
        "cancel_id": rx["cancel_id"].values, "contract_id": rx["contract_id"].values, "reinstated_at": at,
        "status": rx["status"].values, "created_at": at, "updated_at": at,
    })
    ok = rx[rx["status"] == "APPROVED"]
    to_status = rx["status"].map({"APPROVED": "INFORCE", "VOIDED": "REINSTATEMENT_VOIDED",
                                  "DENIED": "REINSTATEMENT_DENIED"}).values
    hist = status_rows(ids, rx["tenant_id"], rx["contract_id"], at, "CANCELLED", to_status)
    out = {"reinstatement": rein, "contract_status_history": hist}
    if len(ok):
        txn = pd.DataFrame({
            "txn_id": ids.take("transaction", len(ok)), "tenant_id": ok["tenant_id"].values,
            "contract_id": ok["contract_id"].values, "txn_type": "REINSTATE", "posted_at": ok["reinstated_at"].values,
            "amount": ok["coverage_cost_refund"].values, "source_ref": ok["cancel_id"].astype(str).values,
            "created_at": ok["reinstated_at"].values, "updated_at": ok["reinstated_at"].values,
        })
        tmap = txn.set_index("contract_id")[["txn_id", "posted_at"]]
        rev = []
        for frame, table in [(cancel_disb, "disbursement"), (cancel_comm, "commission")]:
            f = frame[frame["contract_id"].isin(tmap.index)].copy()
            f["txn_id"] = f["contract_id"].map(tmap["txn_id"]).values
            f["posted_at"] = f["created_at"] = f["updated_at"] = f["contract_id"].map(tmap["posted_at"]).values
            f["amount"] = -f["amount"]
            f[ac.pk(table)] = ids.take(table, len(f))
            rev.append(f.drop(columns=["row_version", "removed_from_source"], errors="ignore"))
        out.update({"transaction": txn, "disbursement": rev[0], "commission": rev[1]})
    return out


def amend_rows(am: pd.DataFrame, contract: pd.DataFrame, ref: Ref, ids: Ids) -> dict[str, pd.DataFrame]:
    """``am``: contract_id (or None + legacy_contract_ref), tenant_id, amendment_type, effective_date,
    delta_coverage_cost, note, at. A non-zero delta on an Admin Core contract posts an AMEND txn
    whose whole delta goes to the claims reserve."""
    if am.empty:
        return {}
    at = am["at"].to_numpy()
    amend = pd.DataFrame({
        "amendment_id": ids.take("contract_amendment", len(am)), "tenant_id": am["tenant_id"].values,
        "contract_id": am["contract_id"].values, "legacy_contract_ref": am["legacy_contract_ref"].values,
        "amendment_type": am["amendment_type"].values, "effective_date": am["effective_date"].values,
        "delta_coverage_cost": am["delta_coverage_cost"].values, "note": am["note"].values,
        "created_at": at, "updated_at": at,
    })
    post = am[am["contract_id"].notna() & (am["delta_coverage_cost"] != 0)]
    out = {"contract_amendment": amend}
    if len(post):
        txn = pd.DataFrame({
            "txn_id": ids.take("transaction", len(post)), "tenant_id": post["tenant_id"].values,
            "contract_id": post["contract_id"].astype(int).values, "txn_type": "AMEND", "posted_at": post["at"].values,
            "amount": post["delta_coverage_cost"].values, "source_ref": post["amendment_type"].values,
            "created_at": post["at"].values, "updated_at": post["at"].values,
        })
        c = contract.set_index("contract_id").loc[txn["contract_id"]].reset_index()
        structure, entity_id = ceding_entity(c["dealer_id"], c.get("entered_date"), ref)
        holder = np.where(structure.isin(RESERVE_TO_ENTITY), ref.holder_id("DEALER_RESERVE", entity_id),
                          ref.holder_id("OBLIGOR", c["original_obligor_id"]))
        disb = pd.DataFrame({
            "disbursement_id": ids.take("disbursement", len(txn)), "tenant_id": txn["tenant_id"],
            "txn_id": txn["txn_id"], "contract_id": txn["contract_id"], "disbursement_type": "CLAIMS_RESERVE",
            "account_holder_id": holder.astype("int64"), "amount": txn["amount"], "posted_at": txn["posted_at"],
            "is_refundable": 1, "is_renewable": 1, "created_at": txn["posted_at"], "updated_at": txn["posted_at"],
        })
        out.update({"transaction": txn, "disbursement": disb})
    return out


def merge_rows(acc: dict[str, list[pd.DataFrame]], rows: dict[str, pd.DataFrame]) -> None:
    for k, v in rows.items():
        if v is not None and len(v):
            acc.setdefault(k, []).append(v)
