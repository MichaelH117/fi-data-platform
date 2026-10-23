-- One claims fact: per-claim paid/incurred in mart_claim_summary must equal fct_claim_payment_line.
with fact as (
    select claim_key, sum(paid_amount) as paid, sum(paid_amount + pending_amount) as incurred
    from {{ ref('fct_claim_payment_line') }}
    group by 1
)

select s.claim_key, s.paid_amount, f.paid, s.incurred_amount, f.incurred
from {{ ref('mart_claim_summary') }} as s
left join fact as f using (claim_key)
where abs(s.paid_amount - coalesce(f.paid, 0)) > 0.01
   or abs(s.incurred_amount - coalesce(f.incurred, 0)) > 0.01
