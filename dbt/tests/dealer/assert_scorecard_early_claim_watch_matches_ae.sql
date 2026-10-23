-- One dealer early-claim watch rule: the scorecard flag must be the A/E flag of
-- mart_dealer_early_claim_ae for the same dealer-month (the Actuarial page test, seed window), and every
-- flagged dealer-month must have a scorecard row. tests/test_dealer.py checks the A/E mart against
-- actuarial/emergence.py early_claim_by_dealer on the as-of date.
with card as (
    select tenant_key, dealer_key, month_key, is_watch_early_claims from {{ ref('mart_dealer_scorecard') }}
),

ae as (
    select tenant_key, dealer_key, month_key, is_flagged
    from {{ ref('mart_dealer_early_claim_ae') }}
    where dealer_key <> '-1'
)

select coalesce(c.tenant_key, a.tenant_key) as tenant_key, coalesce(c.dealer_key, a.dealer_key) as dealer_key,
    coalesce(c.month_key, a.month_key) as month_key, c.is_watch_early_claims, a.is_flagged
from card as c
full outer join ae as a using (tenant_key, dealer_key, month_key)
where coalesce(c.is_watch_early_claims, false) <> coalesce(a.is_flagged, false)
