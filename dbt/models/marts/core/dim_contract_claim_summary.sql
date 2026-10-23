-- Claim roll-up per reportable contract, DERIVED from fct_claim_payment_line (no second claims
-- fact). Early-claim flags: first loss within 30/60/90/120/150/180 days of the effective date.
with per_claim as (
    select
        c.contract_key,
        c.claim_key,
        c.status_group,
        c.loss_date,
        sum(f.paid_amount) as paid,
        sum(f.paid_amount + f.pending_amount) as incurred
    from {{ ref('dim_claim') }} as c
    left join {{ ref('fct_claim_payment_line') }} as f using (claim_key)
    where c.contract_key <> '-1'
    group by all
),

per_contract as (
    select
        contract_key,
        count(*) as claims_reported,
        count(*) filter (where status_group <> 'denied') as claim_count,
        count(*) filter (where status_group = 'denied') as claims_denied,
        coalesce(sum(paid), 0) as paid_to_date,
        coalesce(sum(incurred), 0) as incurred_to_date,
        min(loss_date) filter (where status_group <> 'denied') as first_loss_date
    from per_claim
    group by 1
)

select
    k.contract_key,
    coalesce(p.claims_reported, 0) as claims_reported,
    coalesce(p.claim_count, 0) as claim_count,
    coalesce(p.claims_denied, 0) as claims_denied,
    round(coalesce(p.paid_to_date, 0), 2) as paid_to_date,
    round(coalesce(p.incurred_to_date, 0), 2) as incurred_to_date,
    p.first_loss_date,
    datediff('day', k.effective_date, p.first_loss_date) as first_claim_days,
    {% for d in [30, 60, 90, 120, 150, 180] %}
    coalesce(datediff('day', k.effective_date, p.first_loss_date) <= {{ d }}, false) as is_early_claim_{{ d }},
    {% endfor %}
    {{ audit_columns() }}
from {{ ref('dim_contract') }} as k
left join per_contract as p using (contract_key)
where k.contract_key <> '-1'
