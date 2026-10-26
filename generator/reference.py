"""Admin Core reference data: tenants, parties, risk entities, product catalog and rate cards.

Rows that every tenant shares (product catalog, rates, obligors, insurers, reinsurers) carry
``tenant_id = 0`` ("platform"). Everything a tenant owns carries its own tenant_id.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from generator import catalog as cat
from generator import sim

PLATFORM = 0
BAND_LOAD = {"0-30K": 0.8, "30-60K": 1.0, "60K+": 1.35, "ALL": 1.0}
CLASS_LOAD = {"A": (0.9, 0.9), "B": (1.0, 1.0), "C": (1.3, 1.4), "ALL": (1.0, 1.0)}  # (frequency, severity)
INFLATION = 0.03  # rate-card trend per plan year
TERM_WEIGHTS = {1: [1.0], 2: [0.4, 0.6], 3: [0.25, 0.45, 0.30], 4: [0.15, 0.30, 0.35, 0.20],
                5: [0.10, 0.25, 0.25, 0.25, 0.15]}
INSURER_BY_OBLIGOR = {1: 1, 2: 2, 3: 3, 4: 1, 5: 2}
LEGACY_TENANT = 1

# Program type (V6 edit 3): how the risk is carried. It lives on the source rows (insurer, product,
# contract) so filings, disbursement routing and pages read one value. A contract takes its product's.
PROGRAM_TYPES = ("FULL_INSURED", "AGENCY", "PASS_THROUGH")
PROGRAM_TYPE_BY_CATEGORY = {"VSC": "FULL_INSURED", "SPECIALTY": "FULL_INSURED", "GAP": "AGENCY",
                            "LIMITED_WARRANTY": "AGENCY", "MAINTENANCE": "PASS_THROUGH", "ANCILLARY": "PASS_THROUGH"}
INSURER_PROGRAM_TYPE = {1: "FULL_INSURED", 2: "AGENCY", 3: "PASS_THROUGH"}
# Product names (V6 edit 16, source side): "<name> <year> - <SUFFIX>". The suffix is the program
# suffix; a "- DNU" (do not use) twin of the first plan year reuses the same product code and has no
# plans, so it can never be sold. Staging derives is_do_not_use from the suffix (not emitted here).
PROGRAM_SUFFIX = {"FULL_INSURED": "FI", "AGENCY": "AG", "PASS_THROUGH": "PT"}
DNU_SUFFIX = "DNU"
# Reinsurance run-off (V6 edit 13, source side). One reinsurer stops taking new business on this date;
# its participation entities go to run-off with it: no contract entered after the date cedes to them.
RUNOFF_REINSURER_ID = 3
RUNOFF_DATE = "2025-07-01"
REINSURER_CESSION_TYPE = {1: "Q", 2: "Q", 3: "X"}  # Q = quota share, X = excess of loss
CESSION_TYPE = {"CFC": "C", "NCFC": "N", "PORC": "P", "RETRO": "R", "PROFIT_SHARE": "S", "DEALER_OWNED_OBLIGOR": "D"}
GL_CLASS = {"C": "4110", "N": "4120", "P": "4130", "R": "4140", "S": "4150", "D": "4160", "Q": "4210", "X": "4220"}


def obligor_for_state(state: str) -> int:
    for oid, _, states in cat.OBLIGORS:
        if states and state in states:
            return oid
    return cat.OBLIGORS[-1][0]


def claim_grace_days(s: cat.Subcategory) -> int:
    """Days after the waiting period in which a loss already in progress is still reviewed (admin header
    field). Vehicle-rated VSC and specialty lines carry a 10-day grace; flat ancillary products none."""
    return 10 if s.rated_by_vehicle or s.category == "SPECIALTY" else 0


def _stamp(created: str, n: int) -> dict[str, list[str]]:
    return {"created_at": [created] * n, "updated_at": [created] * n}


def place_names(rng: np.random.Generator) -> list[str]:
    """Every two-word place name from the catalog word lists, in a seeded order (no duplicates)."""
    names = [f"{a} {b}" for a in cat.PLACE_WORDS_A for b in cat.PLACE_WORDS_B if a != b]
    return [names[i] for i in rng.permutation(len(names))]


# Participation-entity name per structure: codes stay upper case (CFC, not "Cfc").
ENTITY_NAME = {
    "CFC": "{stem} Reinsurance Ltd (CFC)", "NCFC": "{stem} Reinsurance Ltd (NCFC)",
    "PORC": "{stem} Reinsurance Ltd (PORC)", "RETRO": "{stem} Retro Reinsurance Ltd",
    "PROFIT_SHARE": "{stem} Profit Share LLC", "DEALER_OWNED_OBLIGOR": "{stem} Dealer Obligor LLC",
}


def name_stem(business_name: str) -> str:
    """'Cedar Ridge Motors' -> 'Cedar Ridge' (the place-word part of a generated business name)."""
    return " ".join(business_name.split()[:2])


def build_reference(seed: int, created: str) -> tuple[dict[str, pd.DataFrame], dict]:
    """Return (tables, dealer profiles). Same seed => identical rows."""
    rng = np.random.default_rng([seed, 1])
    unique = iter(place_names(np.random.default_rng([seed, 2])))  # dealers, groups, shops, agencies
    cities = place_names(np.random.default_rng([seed, 3]))
    t: dict[str, pd.DataFrame] = {}

    t["administrator"] = pd.DataFrame(
        [dict(tenant_id=i, administrator_code=c, reportable_name=n, logo_path=lp, primary_color=p, secondary_color=s,
              admin_company_id=1)  # V7 edit 45: each tenant's instance calls itself company 1
         for i, c, n, lp, p, s, _ in cat.TENANTS]
    )
    tenant_ids = [x[0] for x in cat.TENANTS]
    tenant_share = np.array([x[6] for x in cat.TENANTS])

    # ---- agents (per tenant), dealer groups, dealers
    agent_tenant = np.repeat(tenant_ids, [7, 3, 2])
    regions = ["South Central", "Southeast", "West", "Northeast", "Midwest", "Mid-Atlantic"]
    agents = []
    for i, ten in enumerate(agent_tenant):
        name = f"{next(unique)} Agency"
        agents.append(dict(agent_id=i + 1, tenant_id=int(ten), agent_code=f"AG{i + 1:02d}", agent_name=name,
                           territory=regions[i % len(regions)],
                           email=f"agent{i + 1:02d}@example.com",
                           is_advance_agreement=int(i % 4 == 0)))
    t["agent"] = pd.DataFrame(agents)

    groups = []
    for i in range(cat.N_DEALER_GROUPS):
        ten = tenant_ids[int(rng.choice(3, p=tenant_share))] if i >= 3 else tenant_ids[i]
        groups.append(dict(dealer_group_id=i + 1, tenant_id=ten, group_code=f"G{i + 1:03d}",
                           group_name=f"{next(unique)} Automotive Group"))
    t["dealer_group"] = pd.DataFrame(groups)

    n = cat.N_DEALERS
    states = list(cat.STATES) + list(
        rng.choice(cat.STATES, n - len(cat.STATES), p=np.array(cat.STATE_WEIGHTS) / sum(cat.STATE_WEIGHTS))
    )
    states = list(rng.permutation(states))
    state_w = dict(zip(cat.STATES, cat.STATE_WEIGHTS, strict=True))
    per_state = pd.Series(states).value_counts().to_dict()
    tenants = rng.choice(tenant_ids, n, p=tenant_share)
    tenants[:3] = tenant_ids  # every tenant has dealers
    legacy_start = np.datetime64(cat.LEGACY_START)
    late = set(rng.permutation(n)[: int(n * 0.18)].tolist())  # onboarded during the book
    dealers, profiles = [], {}
    for i in range(n):
        ten, st = int(tenants[i]), states[i]
        ten_groups = t["dealer_group"].loc[t["dealer_group"]["tenant_id"] == ten, "dealer_group_id"].to_numpy()
        ten_agents = t["agent"].loc[t["agent"]["tenant_id"] == ten, "agent_id"].to_numpy()
        number = f"D{1001 + i}"
        name = f"{next(unique)} {cat.DEALER_SUFFIXES[i % len(cat.DEALER_SUFFIXES)]}"
        if i in late:
            onboard = legacy_start + int(rng.integers(120, 2300)) * sim.DAY
        else:
            onboard = legacy_start - int(rng.integers(30, 900)) * sim.DAY
        rule = "WEEKLY" if rng.random() < 0.6 else "MONTHLY"
        dealers.append(dict(
            dealer_id=i + 1, tenant_id=ten, dealer_number=number, dealer_name=name,
            dealer_group_id=int(rng.choice(ten_groups)) if rng.random() < 0.55 else None,
            agent_id=int(rng.choice(ten_agents)), city=cities[i % len(cities)], state=st, status="ACTIVE",
            onboarded_date=str(onboard), is_demo=0, remit_rule=rule, weekly_remit_flag=int(rule == "WEEKLY"),
            program="PREFERRED" if rng.random() < 0.25 else "RETAIL"))
        late_remitter = rng.random() < 0.15
        profiles[number] = {
            "weight": float(rng.lognormal(0, 0.6) * state_w[st] / per_state[st]),
            "markup": float(np.clip(rng.normal(1.75, 0.15), 1.35, 2.3)),
            "loss_factor": float(rng.lognormal(0, 0.15)),
            "cancel_factor": float(rng.lognormal(0, 0.15)),
            "remit_delay": (15, 60) if late_remitter else (0, 4),
            "portal_remitter": bool(rng.random() < 0.1),
        }
    by_size = sorted(profiles, key=lambda c: -profiles[c]["weight"])
    for code in rng.choice(by_size[:40], 6, replace=False):  # clearly bad loss ratio
        profiles[code]["loss_factor"] = float(rng.uniform(1.8, 2.3))
        profiles[code]["cancel_factor"] = float(rng.uniform(1.3, 1.6))
    for code in rng.choice(by_size[40:], 5, replace=False):  # best in class
        profiles[code]["loss_factor"] = float(rng.uniform(0.55, 0.7))
    for j in range(cat.N_DEMO_DEALERS):  # demo/training dealers exist by design and are never reportable
        number = f"D{9001 + j}"
        dealers.append(dict(
            dealer_id=n + j + 1, tenant_id=tenant_ids[j % 3], dealer_number=number,
            dealer_name=f"Demo Dealer {j + 1} (Training)", dealer_group_id=None,
            agent_id=int(t["agent"].loc[t["agent"]["tenant_id"] == tenant_ids[j % 3], "agent_id"].iloc[0]),
            city="Testville", state=cat.STATES[j], status="ACTIVE", onboarded_date=str(legacy_start), is_demo=1,
            remit_rule="WEEKLY", weekly_remit_flag=1, program="RETAIL"))
        profiles[number] = {"weight": 0.0015, "markup": 1.5, "loss_factor": 1.0, "cancel_factor": 1.0,
                            "remit_delay": (0, 2), "portal_remitter": False}
    t["dealer"] = pd.DataFrame(dealers)

    t["lienholder"] = pd.DataFrame(
        [dict(lienholder_id=k * len(cat.LIENHOLDERS) + i + 1, tenant_id=ten, lienholder_code=f"LH{i + 1:02d}",
              lienholder_name=name)
         for k, ten in enumerate(tenant_ids) for i, name in enumerate(cat.LIENHOLDERS)]
    )

    # ---- risk entities (platform rows)
    t["obligor"] = pd.DataFrame(
        [dict(obligor_id=o, tenant_id=PLATFORM, obligor_name=nm, state=(s[0] if s else "DE"))
         for o, nm, s in cat.OBLIGORS]
    )
    t["insurer"] = pd.DataFrame([dict(insurer_id=i, tenant_id=PLATFORM, insurer_name=nm,
                                      program_type=INSURER_PROGRAM_TYPE[i]) for i, nm in cat.INSURERS])
    t["reinsurer"] = pd.DataFrame(
        [dict(reinsurer_id=i, tenant_id=PLATFORM, reinsurer_name=nm,
              status="RUNOFF" if i == RUNOFF_REINSURER_ID else "ACTIVE",
              runoff_date=RUNOFF_DATE if i == RUNOFF_REINSURER_ID else None,
              cession_type=REINSURER_CESSION_TYPE[i], gl_class_code=f"GL-{GL_CLASS[REINSURER_CESSION_TYPE[i]]}")
         for i, nm in cat.REINSURERS]
    )
    real = t["dealer"][t["dealer"]["is_demo"] == 0]
    chosen = real.sample(frac=cat.PARTICIPATION_SHARE, random_state=int(rng.integers(1 << 31))).sort_values("dealer_id")
    kinds = sim.weighted_choice(rng, list(cat.STRUCTURE_MIX), list(cat.STRUCTURE_MIX.values()), len(chosen))
    t["participation_entity"] = pd.DataFrame({
        "entity_id": np.arange(1, len(chosen) + 1),
        "tenant_id": chosen["tenant_id"].to_numpy(),
        "dealer_id": chosen["dealer_id"].to_numpy(),
        "entity_name": [ENTITY_NAME[k].format(stem=name_stem(nm)) for nm, k in
                        zip(chosen["dealer_name"], kinds, strict=True)],
        "structure_type": kinds,
        "reinsurer_id": [int(rng.integers(1, 4)) if k in ("CFC", "NCFC", "PORC", "RETRO") else None for k in kinds],
        "effective_date": chosen["onboarded_date"].to_numpy(),
        "profit_share_pct": [0.5 if k in ("PROFIT_SHARE", "RETRO") else None for k in kinds],
    })
    pe = t["participation_entity"]
    runoff = pe["reinsurer_id"].eq(RUNOFF_REINSURER_ID).to_numpy()
    pe["status"] = np.where(runoff, "RUNOFF", "ACTIVE")
    pe["runoff_date"] = np.where(runoff, RUNOFF_DATE, None)
    pe["cession_type"] = pe["structure_type"].map(CESSION_TYPE)
    pe["gl_class_code"] = "GL-" + pe["cession_type"].map(GL_CLASS)

    # ---- account holders
    holders = []

    def holder(ten, kind, name, state=None, ref=None):
        holders.append(dict(account_holder_id=len(holders) + 1, tenant_id=ten, holder_type=kind, holder_name=name,
                            state=state, ref_id=ref))

    for ten, _code, name, *_ in cat.TENANTS:
        holder(ten, "ADMIN", f"{name} (admin)", ref=ten)
    for i, nm in cat.INSURERS:
        holder(PLATFORM, "INSURER", nm, ref=i)
    for o, nm, s in cat.OBLIGORS:
        holder(PLATFORM, "OBLIGOR", nm, state=s[0] if s else "DE", ref=o)
    for r in t["participation_entity"].itertuples():
        holder(r.tenant_id, "DEALER_RESERVE", r.entity_name, ref=r.entity_id)
    for r in t["agent"].itertuples():
        holder(r.tenant_id, "AGENT", r.agent_name, ref=r.agent_id)
    for k in range(cat.N_REPAIR_FACILITIES):
        st = cat.STATES[k % len(cat.STATES)] if k < len(cat.STATES) else str(
            rng.choice(cat.STATES, p=np.array(cat.STATE_WEIGHTS) / sum(cat.STATE_WEIGHTS)))
        holder(PLATFORM, "REPAIR_FACILITY", f"{next(unique)} {cat.REPAIR_SUFFIXES[k % 3]}",
               state=st, ref=k + 1)
    t["account_holder"] = pd.DataFrame(holders)

    # ---- products and rates (platform rows)
    cats_ = sorted({s.category for s in cat.SUBCATEGORIES}, key=[s.category for s in cat.SUBCATEGORIES].index)
    t["product_category"] = pd.DataFrame(
        [dict(category_id=i + 1, tenant_id=PLATFORM, category_code=c,
              category_name=c if c in ("VSC", "GAP") else c.replace("_", " ").title())  # keep acronyms upper case
         for i, c in enumerate(cats_)]
    )
    cat_id = {c: i + 1 for i, c in enumerate(cats_)}
    t["product_subcategory"] = pd.DataFrame(
        [dict(subcategory_id=i + 1, tenant_id=PLATFORM, category_id=cat_id[s.category], subcategory_code=s.code,
              subcategory_name=s.name) for i, s in enumerate(cat.SUBCATEGORIES)]
    )
    products, plans, programs, rates, comps = [], [], [], [], []
    for si, s in enumerate(cat.SUBCATEGORIES):
        ptype = PROGRAM_TYPE_BY_CATEGORY[s.category]
        for year in cat.PLAN_YEARS:
            pid = len(products) + 1
            pname = cat.BUNDLE_PRODUCT_NAMES.get((s.code, year), s.name)  # V7 edit 50: bundle names
            products.append(dict(product_id=pid, tenant_id=PLATFORM, subcategory_id=si + 1, product_code=s.code,
                                 product_name=f"{pname} {year} - {PROGRAM_SUFFIX[ptype]}", contract_prefix=s.prefix,
                                 plan_year=year, is_cancellable=int(s.cancellable), claim_wait_days=s.wait_days,
                                 claim_wait_miles=s.wait_miles, coverage_limit=s.coverage_limit,
                                 program_type=ptype))
            trend = (1 + INFLATION) ** (year - cat.PLAN_YEARS[0])
            for ded in s.deductibles:
                plan_id = len(plans) + 1
                plans.append(dict(plan_id=plan_id, tenant_id=PLATFORM, product_id=pid,
                                  plan_code=f"{s.code}-{year}-D{int(ded)}",
                                  plan_name=f"{s.name} {year} ${int(ded)} ded",
                                  deductible_amount=ded,
                                  deductible_name="DISAPPEARING" if ded == 0 and s.category == "VSC"
                                  else f"${int(ded)} PER VISIT"))
                for prog in cat.PROGRAMS:
                    programs.append(dict(program_id=len(programs) + 1, tenant_id=PLATFORM, plan_id=plan_id,
                                         program_code=prog, program_name=f"{prog.title()} program"))
                bands = [b[2] for b in cat.ODOMETER_BANDS] if s.rated_by_vehicle else ["ALL"]
                classes = ["A", "B", "C"] if s.rated_by_vehicle else ["ALL"]
                for term in s.terms:
                    for band in bands:
                        for klass in classes:
                            f_load, s_load = CLASS_LOAD[klass]
                            lam = s.annual_claim_rate * term / 12 * BAND_LOAD[band] * f_load
                            net_sev = max(s.severity * s_load * trend - ded, s.severity * 0.15)
                            reserve = round(lam * net_sev / cat.TARGET_LOSS_RATIO, 0)
                            insurer = round(reserve * cat.INSURER_FEE_PCT, 2)
                            tax = round((reserve + insurer) * cat.PREMIUM_TAX_PCT, 2)
                            psr = round(reserve * cat.PROFIT_SHARE_PCT, 2)
                            parts = {"ADMIN_FEE": s.admin_fee, "INSURER_FEE": insurer, "PREMIUM_TAX": tax,
                                     "COMMISSION": s.commission, "CLAIMS_RESERVE": reserve,
                                     "PROFIT_SHARE_RESERVE": psr}
                            cost = round(sum(parts.values()), 2)
                            rid = len(rates) + 1
                            rates.append(dict(rate_id=rid, tenant_id=PLATFORM, plan_id=plan_id,
                                              coverage_level=s.name.upper(), term_months=term,
                                              term_miles=term * s.miles_per_month or None, odometer_band=band,
                                              class_code=klass, deductible_amount=ded, coverage_cost=cost,
                                              suggested_retail=float(round(cost * 1.9, -1) - 1)))
                            for kind, amt in parts.items():
                                comps.append(dict(rate_component_id=len(comps) + 1, tenant_id=PLATFORM, rate_id=rid,
                                                  disbursement_type=kind, account_holder_id=None, amount=amt,
                                                  is_refundable=int(kind != "ADMIN_FEE"),
                                                  is_renewable=int(kind in ("CLAIMS_RESERVE", "ADMIN_FEE"))))
    for p in [p for p in products if p["plan_year"] == cat.PLAN_YEARS[0]]:  # do-not-use twins: no plans, never sold
        products.append({**p, "product_id": len(products) + 1,
                         "product_name": p["product_name"].rsplit(" - ", 1)[0] + f" - {DNU_SUFFIX}"})
    t["product"], t["plan"], t["program"] = pd.DataFrame(products), pd.DataFrame(plans), pd.DataFrame(programs)
    t["rate"], t["rate_component"] = pd.DataFrame(rates), pd.DataFrame(comps)

    # ---- users
    roles = ["ADJUSTER"] * 14 + ["CSR"] * 4 + ["ACCOUNTING"] * 3 + ["MANAGER"] * 3
    users = []
    for i in range(cat.N_ADMIN_USERS):  # role-based synthetic users, never person names
        name = f"{roles[i].title()} {i + 1:02d}"
        users.append(dict(admin_user_id=i + 1, tenant_id=tenant_ids[i % 3], user_name=name, role=roles[i],
                          email=f"{roles[i].lower()}.{i + 1:02d}@example.com"))
    t["admin_user"] = pd.DataFrame(users)
    portal = []
    for ten, code, *_ in cat.TENANTS:
        portal.append(dict(portal_user_id=len(portal) + 1, tenant_id=ten, dealer_id=None, role="TENANT_ADMIN",
                           login=f"{code.lower()}.admin"))
    for d in t["dealer"].itertuples():
        for k in range(1 + int(d.dealer_id % 3 == 0)):
            portal.append(dict(portal_user_id=len(portal) + 1, tenant_id=d.tenant_id, dealer_id=d.dealer_id,
                               role="DEALER_ADMIN" if k == 0 else "DEALER_VIEWER",
                               login=f"{d.dealer_number.lower()}.{['admin', 'viewer'][k]}"))
    t["portal_user"] = pd.DataFrame(portal)

    for name, df in t.items():
        for k, v in _stamp(created, len(df)).items():
            df[k] = v
        t[name] = df
    return t, profiles


def onboard_day(dealers: pd.DataFrame) -> np.ndarray:
    return sim.parse_days(dealers["onboarded_date"])


def as_date(d: date | str) -> np.datetime64:
    return np.datetime64(str(d), "D")
