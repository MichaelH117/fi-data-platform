-- Reconciliation gate: the dealer dimension agrees with the admin-API dealer roster (an independent extract
-- of the admin system's dealer master). Fails on any reportable dealer missing from dim_dealer, any
-- Admin Core dealer missing from the roster, or any tenant / state / status / name / remit-rule
-- mismatch. Quarantined API records, demo dealers and legacy-only dealers are reported in
-- int_dealer_roster_drift but do not fail the gate. One row per drifting dealer.
select dealer_number, drift_type, mismatched_attributes
from {{ ref('int_dealer_roster_drift') }}
where drift_type in ('MISSING_IN_DIM', 'MISSING_IN_API', 'ATTRIBUTE_MISMATCH')
