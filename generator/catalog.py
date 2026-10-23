"""Static reference data and economic assumptions for the synthetic admin systems.

Everything here is invented. The numbers are chosen to look like a plausible vehicle
service contract (VSC) administrator book. Dealer cost (``coverage_cost``) is the sum of the
rate components (admin fee, insurer fee, premium tax, commission, claims reserve and
profit-share reserve). The customer pays ``purchase_price`` = coverage cost x a dealer markup.
The claims reserve is priced so an average dealer runs near ``TARGET_LOSS_RATIO``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_SEED = 42
LEGACY_START = date(2019, 1, 1)
CUTOVER = date(2023, 1, 1)  # contracts entered on/after this date live in Admin Core
DEFAULT_AS_OF = date(2026, 10, 21)
DEFAULT_LEGACY_CONTRACTS = 20_000
DEFAULT_CORE_CONTRACTS = 40_000
LANDING_DAYS = 3  # the last N business days arrive as landing files, not as Admin Core rows

# --------------------------------------------------------------------------- geography
# All 50 states + DC. Ten large states carry about 70% of the volume.
BIG_STATES = {
    "TX": 0.12, "FL": 0.11, "CA": 0.09, "GA": 0.07, "NC": 0.06,
    "AZ": 0.05, "OH": 0.05, "PA": 0.05, "IL": 0.05, "NY": 0.05,
}
OTHER_STATES = [
    "AL", "AK", "AR", "CO", "CT", "DE", "DC", "HI", "ID", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "ND", "OK", "OR", "RI", "SC",
    "SD", "TN", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]
STATES = list(BIG_STATES) + OTHER_STATES
STATE_WEIGHTS = list(BIG_STATES.values()) + [0.30 / len(OTHER_STATES)] * len(OTHER_STATES)
# Two-digit ZIP prefix range per state (approximate, for plausible fake ZIP codes only).
ZIP_PREFIX = {
    "AL": (35, 36), "AK": (99, 99), "AZ": (85, 86), "AR": (71, 72), "CA": (90, 96), "CO": (80, 81),
    "CT": (6, 6), "DE": (19, 19), "DC": (20, 20), "FL": (32, 34), "GA": (30, 31), "HI": (96, 96),
    "ID": (83, 83), "IL": (60, 62), "IN": (46, 47), "IA": (50, 52), "KS": (66, 67), "KY": (40, 42),
    "LA": (70, 71), "ME": (4, 4), "MD": (20, 21), "MA": (1, 2), "MI": (48, 49), "MN": (55, 56),
    "MS": (38, 39), "MO": (63, 65), "MT": (59, 59), "NE": (68, 69), "NV": (88, 89), "NH": (3, 3),
    "NJ": (7, 8), "NM": (87, 88), "NY": (10, 14), "NC": (27, 28), "ND": (58, 58), "OH": (43, 45),
    "OK": (73, 74), "OR": (97, 97), "PA": (15, 19), "RI": (2, 2), "SC": (29, 29), "SD": (57, 57),
    "TN": (37, 38), "TX": (75, 79), "UT": (84, 84), "VT": (5, 5), "VA": (22, 24), "WA": (98, 99),
    "WV": (24, 26), "WI": (53, 54), "WY": (82, 83),
}

# --------------------------------------------------------------------------- tenants and parties
TENANTS = [  # white-label administrators (tenant_id, code, reportable name, logo, colors, dealer share)
    (1, "PVA", "Provider A Vehicle Services", "static/tenants/pva.svg", "#1F4E79", "#F2A900", 0.60),
    (2, "NGT", "Northgate Protection Administrators", "static/tenants/ngt.svg", "#2E7D32", "#B0BEC5", 0.25),
    (3, "LKS", "Lakeshore Service Contract Co.", "static/tenants/lks.svg", "#6A1B9A", "#FFC107", 0.15),
]
N_DEALERS = 150
N_DEMO_DEALERS = 3
N_DEALER_GROUPS = 24
N_AGENTS = 12
N_ADMIN_USERS = 24
DEALER_SUFFIXES = ["Motors", "Auto Group", "Automotive", "Auto Sales", "Car Center", "Autoplex", "Imports"]
# Business names (dealers, dealer groups, repair shops, agencies, cities) are built from these neutral
# place and nature words, never from a person-name generator, so no synthetic business can carry a
# real person's name. tests/test_generator.py checks that no word is a Faker first or last name.
PLACE_WORDS_A = [
    "Cedar", "Pine", "Oak", "Maple", "Willow", "Aspen", "Birch", "Elm", "Juniper", "Spruce", "Cypress", "Hickory",
    "Magnolia", "Sycamore", "Alder", "Laurel", "Red", "Blue", "Silver", "Granite", "Iron", "Copper", "Sun",
    "North", "South", "East", "High", "Clear", "Fair", "Deep", "Twin", "Eagle", "Falcon", "Hawk", "Bison", "Elk",
    "Heron", "Prairie", "Mesa", "Harbor", "River", "Lake", "Bay", "Coral", "Summit", "Timber", "Quarry", "Orchard",
]
PLACE_WORDS_B = [
    "Ridge", "Creek", "Valley", "Lake", "Harbor", "Point", "Hills", "Field", "Brook", "Springs", "Crossing",
    "Grove", "Bend", "Meadow", "Mesa", "View", "Rock", "Run", "Hollow", "Landing", "Shore", "Trail", "Gate",
    "Bluff", "Plains", "Flats", "Station",
]
REPAIR_SUFFIXES = ["Auto Repair", "Service Center", "Garage"]
OBLIGORS = [  # obligor_id, name, states it backs (None = every remaining state)
    (1, "Obligor 1 Service Corp", ["TX", "OK", "LA", "AR", "NM"]),
    (2, "Obligor 2 Warranty Co", ["FL", "GA", "AL", "SC", "MS", "TN"]),
    (3, "Obligor 3 Contract Services", ["CA", "AZ", "NV", "OR", "WA", "UT", "ID", "HI", "AK"]),
    (4, "Obligor 4 Protection LLC", ["NY", "PA", "NJ", "CT", "MA", "MD", "DE", "DC", "VA", "RI", "VT", "NH", "ME"]),
    (5, "Obligor 5 Vehicle Plans", None),
]
# Contracts in these states moved from the original obligor to obligor 5 from this date on.
OBLIGOR_TRANSFER = {"states": ["NV", "UT", "OR"], "from_date": date(2024, 7, 1), "to_obligor": 5}
INSURERS = [(1, "Insurer North"), (2, "Insurer South"), (3, "Insurer Central")]
REINSURERS = [(1, "Reinsurer One Ltd"), (2, "Reinsurer Two Re"), (3, "Harbor Re Group")]
STRUCTURE_MIX = {"PROFIT_SHARE": 0.30, "CFC": 0.20, "NCFC": 0.15, "PORC": 0.15, "RETRO": 0.12,
                 "DEALER_OWNED_OBLIGOR": 0.08}
PARTICIPATION_SHARE = 0.40  # share of dealers with a participation entity
LIENHOLDERS = [
    "First Lakes Credit Union", "Pinecrest Auto Finance", "Union Harbor Bank", "Meridian Motor Credit",
    "Redwood Lending", "Bluewater Financial", "Granite State Credit", "Prairie Auto Loans",
    "Coastal Community Bank", "Summit Finance Co", "Keystone Auto Credit", "Desert Sun Lending",
]
N_REPAIR_FACILITIES = 220

# --------------------------------------------------------------------------- vehicles
VEHICLES = {  # make -> (class code, models)
    "Toyota": ("A", ["Camry", "RAV4", "Tacoma", "Highlander"]),
    "Honda": ("A", ["Accord", "Civic", "CR-V", "Pilot"]),
    "Hyundai": ("A", ["Elantra", "Tucson", "Santa Fe", "Sonata"]),
    "Kia": ("A", ["Sorento", "Sportage", "Forte", "Telluride"]),
    "Mazda": ("A", ["Mazda3", "CX-5", "CX-30", "BT-50"]),
    "Subaru": ("A", ["Outback", "Forester", "Crosstrek", "Ascent"]),
    "Ford": ("B", ["F-150", "Escape", "Explorer", "Super Duty"]),
    "Chevrolet": ("B", ["Silverado", "Equinox", "Malibu", "Tahoe"]),
    "Jeep": ("B", ["Wrangler", "Grand Cherokee", "Cherokee", "Compass"]),
    "Ram": ("B", ["1500", "2500", "ProMaster", "3500"]),
    "GMC": ("B", ["Sierra", "Acadia", "Terrain", "Yukon"]),
    "BMW": ("C", ["X3", "X5", "3 Series", "5 Series"]),
    "Audi": ("C", ["Q5", "A4", "Q7", "A6"]),
    "Tesla": ("C", ["Model 3", "Model Y", "Model S", "Model X"]),
}
MAKE_WEIGHTS = [0.13, 0.11, 0.07, 0.06, 0.08, 0.05, 0.13, 0.11, 0.07, 0.06, 0.05, 0.03, 0.02, 0.03]
RV_CHASSIS_MAKES = ["Ford", "Freightliner", "Ram"]
RV_COACH_MAKES = {"Coachline": ["Voyager 30", "Voyager 34"], "Roadhaven": ["Class C 24", "Class A 36"]}
POWERSPORTS_MAKES = {"Yamaha": ["Grizzly", "YZF"], "Kawasaki": ["Ninja", "Teryx"], "Honda": ["Talon", "Gold Wing"]}
ODOMETER_BANDS = [(0, 30_000, "0-30K"), (30_000, 60_000, "30-60K"), (60_000, 10**9, "60K+")]
SURCHARGES = {  # code -> (amount, rule used to decide which contracts must carry it)
    "DIESEL": 150.0, "4X4": 75.0, "TURBO": 100.0, "HYBRID_EV": 125.0, "COMMERCIAL": 250.0, "BRANDED_TITLE": 200.0,
}
OPTIONS = {"SEALS_GASKETS": 75.0, "WEAR_TEAR": 120.0, "ROADSIDE": 40.0, "RENTAL": 60.0, "HIGH_TECH": 150.0}


# --------------------------------------------------------------------------- products
@dataclass(frozen=True)
class Subcategory:
    code: str  # product_code, also the contract-number prefix when ``prefix`` is empty
    category: str
    name: str
    prefix: str
    mix: float  # share of new sales
    terms: tuple[int, ...]
    miles_per_month: int  # term_miles = term_months x this (0 = unlimited)
    annual_claim_rate: float  # claims per in-force year (new vehicle, average dealer)
    severity: float  # mean gross repair cost per claim
    admin_fee: float
    commission: float
    deductibles: tuple[float, ...] = (100.0, 0.0)
    wait_days: int = 30
    wait_miles: int = 1000
    coverage_limit: float = 10_000.0
    cancellable: bool = True
    rated_by_vehicle: bool = False  # VSC-style rate cells (odometer band x class code)
    components: tuple[str, ...] = field(default=())


SUBCATEGORIES = [
    Subcategory("VSC-PT", "VSC", "Powertrain", "VSC", 0.17, (24, 36, 48, 60, 72), 1250, 0.07, 2400, 85, 100,
                rated_by_vehicle=True, components=("ENGINE", "TRANSMISSION", "DRIVE_AXLE", "TURBO")),
    Subcategory("VSC-SC", "VSC", "Stated Component", "VSC", 0.14, (24, 36, 48, 60, 72), 1250, 0.10, 1500, 90, 115,
                rated_by_vehicle=True, components=("ENGINE", "TRANSMISSION", "AIR_CONDITIONING", "STEERING",
                                                   "COOLING", "FUEL_SYSTEM")),
    Subcategory("VSC-EN", "VSC", "Enhanced", "VSC", 0.13, (36, 48, 60, 72), 1250, 0.13, 1250, 95, 125,
                rated_by_vehicle=True, components=("ENGINE", "TRANSMISSION", "AIR_CONDITIONING", "ELECTRICAL",
                                                   "STEERING", "SUSPENSION", "COOLING")),
    Subcategory("VSC-EX", "VSC", "Exclusionary", "VSC", 0.11, (36, 48, 60, 72, 84), 1250, 0.16, 1100, 110, 150,
                deductibles=(100.0, 50.0, 0.0), rated_by_vehicle=True,
                components=("ENGINE", "TRANSMISSION", "ELECTRICAL", "HI_TECH_ELECTRONICS", "SUSPENSION",
                            "SEALS_GASKETS", "AIR_CONDITIONING")),
    Subcategory("GAP", "GAP", "GAP", "GAP", 0.13, (48, 60, 72, 84), 0, 0.012, 3800, 60, 90, deductibles=(0.0,),
                wait_days=0, wait_miles=0, coverage_limit=50_000.0, components=("TOTAL_LOSS_DEFICIENCY",)),
    Subcategory("LTW", "LIMITED_WARRANTY", "Limited Warranty", "LTW", 0.03, (3, 6, 12), 1000, 0.25, 900, 40, 30,
                deductibles=(100.0,), coverage_limit=3_000.0, components=("ENGINE", "TRANSMISSION")),
    Subcategory("PPM", "MAINTENANCE", "Prepaid Maintenance", "PPM", 0.06, (24, 36, 48), 1250, 0.55, 95, 35, 45,
                deductibles=(0.0,), wait_days=0, wait_miles=0, coverage_limit=150.0, cancellable=False,
                components=("OIL_CHANGE", "TIRE_ROTATION", "MULTI_POINT_INSPECTION")),
    Subcategory("TW", "ANCILLARY", "Tire & Wheel", "TW", 0.05, (36, 60), 0, 0.14, 320, 35, 40, deductibles=(0.0,),
                wait_days=0, wait_miles=0, coverage_limit=1_500.0, components=("TIRE", "WHEEL")),
    Subcategory("KEY", "ANCILLARY", "Key Replacement", "KEY", 0.03, (36, 60), 0, 0.05, 380, 25, 30,
                deductibles=(0.0,), wait_days=0, wait_miles=0, coverage_limit=800.0, cancellable=False,
                components=("KEY_FOB",)),
    Subcategory("PDR", "ANCILLARY", "Paintless Dent Repair", "PDR", 0.02, (36, 60), 0, 0.18, 210, 25, 30,
                deductibles=(0.0,), wait_days=0, wait_miles=0, coverage_limit=1_000.0, components=("DENT",)),
    Subcategory("WS", "ANCILLARY", "Windshield", "WS", 0.02, (36, 60), 0, 0.10, 420, 25, 30, deductibles=(0.0,),
                wait_days=0, wait_miles=0, coverage_limit=1_200.0, components=("WINDSHIELD",)),
    Subcategory("APP", "ANCILLARY", "Appearance Protection", "APP", 0.02, (36, 60), 0, 0.06, 260, 30, 35,
                deductibles=(0.0,), wait_days=0, wait_miles=0, coverage_limit=1_000.0,
                components=("INTERIOR_STAIN", "PAINT")),
    Subcategory("ETCH", "ANCILLARY", "Theft Etch", "ETCH", 0.01, (60,), 0, 0.004, 2500, 20, 25, deductibles=(0.0,),
                wait_days=0, wait_miles=0, coverage_limit=5_000.0, cancellable=False, components=("THEFT_BENEFIT",)),
    Subcategory("TECH", "ANCILLARY", "Technology Protection", "TECH", 0.02, (36, 60), 1250, 0.09, 700, 35, 40,
                deductibles=(50.0,), coverage_limit=2_500.0, components=("INFOTAINMENT", "SENSORS", "CAMERA")),
    Subcategory("BNDL", "ANCILLARY", "Ancillary Bundle", "BNDL", 0.03, (36, 60), 0, 0.30, 330, 60, 70,
                deductibles=(0.0,), wait_days=0, wait_miles=0, coverage_limit=3_000.0,
                components=("TIRE", "WHEEL", "DENT", "KEY_FOB", "WINDSHIELD")),
    Subcategory("RV", "SPECIALTY", "RV Protection", "RV", 0.02, (36, 60, 84), 1000, 0.18, 2600, 140, 180,
                deductibles=(250.0, 100.0), rated_by_vehicle=False,
                components=("CHASSIS_ENGINE", "CHASSIS_TRANSMISSION", "COACH_APPLIANCES", "SLIDE_OUT", "GENERATOR")),
    Subcategory("PSP", "SPECIALTY", "Powersports", "PSP", 0.01, (24, 36), 500, 0.15, 1100, 60, 70,
                deductibles=(100.0,), components=("ENGINE", "TRANSMISSION", "ELECTRICAL")),
]
SUBCAT_BY_CODE = {s.code: s for s in SUBCATEGORIES}
PLAN_YEARS = list(range(2019, 2027))
LEGACY_PRODUCTS_LAST_YEAR = 2021  # stale legacy product table: plan year 2022 rows are missing
LEGACY_SUBCATS = ["VSC-PT", "VSC-SC", "VSC-EN", "VSC-EX", "GAP", "TW"]
PROGRAMS = ["RETAIL", "PREFERRED"]  # program per plan; PREFERRED dealers pay a lower admin fee

TARGET_LOSS_RATIO = 0.68
USED_RISK_LOAD = 1.30
INSURER_FEE_PCT = 0.06  # of the claims reserve
PREMIUM_TAX_PCT = 0.025  # of reserve + insurer fee
PROFIT_SHARE_PCT = 0.05  # of the claims reserve
CANCEL_FEE = 50.0
FREE_LOOK_DAYS = 60  # full refund inside this window
LIFETIME_CANCEL_PROB = 0.22
EARLY_CANCEL_SHARE = 0.55
EARLY_CANCEL_MEAN_DAYS = 75.0
CANCEL_REASONS = {  # reason -> (weight, initiated_by)
    "Customer Request": (0.30, "CUSTOMER"),
    "Payoff/Trade": (0.24, "LENDER"),
    "Total Loss": (0.10, "LENDER"),
    "Repossession": (0.10, "LENDER"),
    "Unwound Deal": (0.14, "DEALER"),
    "Dealer Error": (0.12, "DEALER"),
}
DENIED_REASONS = {  # reason -> category
    "PRE_EXISTING_CONDITION": "ELIGIBILITY",
    "COMPONENT_NOT_COVERED": "COVERAGE",
    "WITHIN_WAITING_PERIOD": "ELIGIBILITY",
    "MAINTENANCE_NEGLECT": "COVERAGE",
    "NO_FAILURE_FOUND": "COVERAGE",
    "MISSING_DOCUMENTATION": "DOCUMENTATION",
}
CLAIM_DENIAL_RATE = 0.10
VIN_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # no I, O, Q
TEST_RANGE_START = 99_990_000  # contract numbers <prefix>-9999xxxx are test contracts by design
