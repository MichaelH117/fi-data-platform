-- mart_claims_kpi has one early_claims_<N> column per window; the windows are a counting rule in
-- seed dealer_claim_kpi_rules. Fails if someone changes the seed without the model (or vice versa).
select rule_value
from {{ ref('dealer_claim_kpi_rules') }}
where rule_id = 'early_claim_windows' and rule_value <> '30|60|90|120|150|180'
